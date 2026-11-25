#!/usr/bin/env python3
"""msgraph-sendmail — send mail through the Microsoft Graph API.

A drop-in replacement for /usr/sbin/sendmail that delivers messages via
Microsoft Graph (https://graph.microsoft.com) instead of SMTP, using an
Entra ID (Azure AD) app registration with application permissions
(client credentials flow).

Two modes of operation:

  sendmail mode (default)
      Reads an RFC 822/MIME message from stdin, exactly like the classic
      sendmail binary.  Understands -t, -f, -i and tolerates the rest of
      the traditional sendmail option zoo, so cron, PHP mail(), logwatch
      and friends work unchanged.

  CLI mode (triggered by --to / --subject / ...)
      Builds the message for you:
      msgraph-sendmail --to user@example.com --subject Hi --body 'Hello'

Only the Python standard library is used.  Python >= 3.8.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import email
import email.policy
import email.utils
import json
import mimetypes
import os
import re
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.0"

PROG = "msgraph-sendmail"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# sendMail requests are capped at ~4 MB by Microsoft Graph.  Larger
# attachments would need a draft message + upload session, which this
# tool does not implement (yet).
MAX_REQUEST_BYTES = 4 * 1024 * 1024

# BSD sysexits(3) — respected by cron, procmail, PHP and most MTAs.
EX_OK = 0
EX_USAGE = 64        # command line usage error
EX_DATAERR = 65      # message could not be parsed / was rejected as invalid
EX_NOUSER = 67       # addressee unknown (local address without an alias)
EX_UNAVAILABLE = 69  # service refused us (authentication, permissions)
EX_TEMPFAIL = 75     # temporary failure, retry later (network, throttling)
EX_CONFIG = 78       # configuration missing or broken

CONFIG_ENV = "MSGRAPH_SENDMAIL_CONFIG"
CONFIG_PATHS = (
    os.path.expanduser("~/.config/msgraph-sendmail.conf"),
    "/etc/msgraph-sendmail.conf",
)

ENV_PREFIX = "MSGRAPH_SENDMAIL_"

DEFAULT_ALIASES_FILE = "/etc/aliases"
MAX_ALIASES_BYTES = 1024 * 1024


class UserError(Exception):
    """Fatal error carrying a sysexits exit code."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_VERBOSE = False

try:
    import syslog as _syslog
except ImportError:  # non-Unix platform
    _syslog = None


def _syslog_write(priority: int, message: str) -> None:
    if _syslog is None:
        return
    try:
        _syslog.openlog(PROG, 0, _syslog.LOG_MAIL)
        _syslog.syslog(priority, message)
    except Exception:
        pass


def log_error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if _syslog is not None:
        _syslog_write(_syslog.LOG_ERR, message)


def log_info(message: str) -> None:
    if _syslog is not None:
        _syslog_write(_syslog.LOG_INFO, message)


def debug(message: str) -> None:
    if _VERBOSE:
        print(f"{PROG}: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.tenant: str | None = None
        self.client_id: str | None = None
        self.client_secret: str | None = None
        self.default_from: str | None = None
        self.save_to_sent: bool = True
        self.timeout: float = 30.0
        self.source: str | None = None  # where the config came from
        # Local address handling (see AddressResolver).
        self.local_recipient: str | None = None
        self.aliases_file: str = DEFAULT_ALIASES_FILE  # "" disables
        self.local_domains: set[str] | None = None     # None = automatic
        self.aliases: dict[str, list[str]] = {}       # [aliases] section


def _config_error_summary(exc: configparser.Error) -> str:
    """Describe a ConfigParser error WITHOUT quoting file content: the
    library's own messages include the offending line or value, which may
    be the client secret, and errors go to stderr and syslog."""
    if isinstance(exc, configparser.InterpolationError):
        return (f"invalid '%' in option {exc.option!r} of section "
                f"[{exc.section}] (write %% for a literal %)")
    if isinstance(exc, configparser.MissingSectionHeaderError):
        return f"line {exc.lineno}: missing section header such as [msgraph]"
    if isinstance(exc, configparser.ParsingError):
        lines = ", ".join(str(error[0]) for error in exc.errors)
        return f"syntax error on line(s) {lines}"
    if isinstance(exc, configparser.DuplicateOptionError):
        return (f"line {exc.lineno}: option {exc.option!r} appears twice "
                f"in section [{exc.section}]")
    if isinstance(exc, configparser.DuplicateSectionError):
        return f"line {exc.lineno}: section [{exc.section}] appears twice"
    return type(exc).__name__


def _read_config_file(path: str, cfg: Config) -> None:
    parser = configparser.ConfigParser()
    # A second, literal view of the file without [DEFAULT] inheritance,
    # used only to learn which options are physically in [aliases].
    literal = configparser.ConfigParser(
        default_section="msgraph-sendmail:no-defaults", interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise UserError(f"cannot read config file {path}: {exc}", EX_CONFIG)
    try:
        parser.read_string(text, source=path)
        literal.read_string(text, source=path)
        _apply_config_section(parser, literal, path, cfg)
    except configparser.Error as exc:
        raise UserError(f"cannot parse config file {path}: "
                        f"{_config_error_summary(exc)}", EX_CONFIG)


def _apply_config_section(parser: configparser.ConfigParser,
                          literal: configparser.ConfigParser, path: str,
                          cfg: Config) -> None:
    if parser.has_section("msgraph"):
        section = parser["msgraph"]
    else:
        section = parser["DEFAULT"]

    cfg.tenant = section.get("tenant", cfg.tenant)
    cfg.client_id = section.get("client_id", cfg.client_id)
    cfg.client_secret = section.get("client_secret", cfg.client_secret)
    secret_file = section.get("client_secret_file")
    if secret_file and not cfg.client_secret:
        try:
            with open(os.path.expanduser(secret_file), encoding="utf-8") as fh:
                cfg.client_secret = fh.read().strip()
        except OSError as exc:
            raise UserError(f"cannot read client_secret_file: {exc}", EX_CONFIG)
    cfg.default_from = section.get("from", cfg.default_from)
    if "save_to_sent" in section:
        try:
            cfg.save_to_sent = section.getboolean("save_to_sent")
        except ValueError:
            raise UserError(
                f"invalid boolean for save_to_sent in {path}", EX_CONFIG)
    if "timeout" in section:
        try:
            cfg.timeout = float(section["timeout"])
        except ValueError:
            raise UserError(f"invalid timeout in {path}", EX_CONFIG)
    if "local_recipient" in section:
        cfg.local_recipient = section["local_recipient"].strip() or None
    if "aliases_file" in section:
        cfg.aliases_file = section["aliases_file"].strip()
    if "local_domains" in section:
        cfg.local_domains = {
            domain.lower()
            for domain in re.split(r"[\s,]+", section["local_domains"])
            if domain
        }
    if literal.has_section("aliases"):
        # Only options written in [aliases] itself — never values
        # inherited from [DEFAULT] (which may hold the client secret).
        for name in literal.options("aliases"):
            value = parser.get("aliases", name)
            cfg.aliases[name.lower()] = _split_alias_targets(value)
    cfg.source = path

    _warn_if_world_readable(path, bool(section.get("client_secret")))


def _warn_if_world_readable(path: str, has_secret: bool) -> None:
    if not has_secret:
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(
            f"{PROG}: warning: {path} contains a client secret but is "
            f"readable by other users — run: chmod 600 {path}",
            file=sys.stderr,
        )


def load_config(explicit_path: str | None) -> Config:
    cfg = Config()

    path = explicit_path or os.environ.get(CONFIG_ENV)
    if path:
        _read_config_file(path, cfg)
    else:
        for candidate in CONFIG_PATHS:
            if os.path.isfile(candidate):
                _read_config_file(candidate, cfg)
                break

    # Environment variables override the config file.
    env = os.environ
    cfg.tenant = env.get(ENV_PREFIX + "TENANT", cfg.tenant)
    cfg.client_id = env.get(ENV_PREFIX + "CLIENT_ID", cfg.client_id)
    cfg.client_secret = env.get(ENV_PREFIX + "CLIENT_SECRET", cfg.client_secret)
    secret_file = env.get(ENV_PREFIX + "CLIENT_SECRET_FILE")
    if secret_file:
        try:
            with open(os.path.expanduser(secret_file), encoding="utf-8") as fh:
                cfg.client_secret = fh.read().strip()
        except OSError as exc:
            raise UserError(f"cannot read client secret file: {exc}", EX_CONFIG)
    cfg.default_from = env.get(ENV_PREFIX + "FROM", cfg.default_from)
    cfg.local_recipient = env.get(ENV_PREFIX + "LOCAL_RECIPIENT",
                                  cfg.local_recipient) or None
    cfg.aliases_file = env.get(ENV_PREFIX + "ALIASES_FILE", cfg.aliases_file)

    cfg.default_from = _single_address(cfg.default_from, "from")
    cfg.local_recipient = _single_address(cfg.local_recipient,
                                          "local_recipient")
    return cfg


def _single_address(value: str | None, key: str) -> str | None:
    """Validate a configured mailbox ("a@b" or "Name <a@b>") and reduce it
    to the bare address: exactly one address with a local part and a
    domain."""
    if not value or not value.strip():
        return None
    parsed = email.utils.getaddresses([value])
    address = parsed[0][1] if len(parsed) == 1 else ""
    local, at, domain = address.rpartition("@")
    if not (at and local and domain):
        raise UserError(f"{key} must be exactly one full address "
                        f"(user@domain), not {value!r}", EX_CONFIG)
    return address


def require_config(cfg: Config) -> None:
    missing = [
        name
        for name, value in (
            ("tenant", cfg.tenant),
            ("client_id", cfg.client_id),
            ("client_secret", cfg.client_secret),
        )
        if not value
    ]
    if missing:
        hint = cfg.source or "no config file found (looked in " + \
            ", ".join(CONFIG_PATHS) + ")"
        raise UserError(
            "missing configuration: " + ", ".join(missing)
            + f" ({hint}); see msgraph-sendmail.conf(5) or README", EX_CONFIG)


# ---------------------------------------------------------------------------
# Command-line parsing
# ---------------------------------------------------------------------------

class Options:
    def __init__(self) -> None:
        self.mode = "sendmail"  # or "cli"
        self.recipients: list[str] = []        # positional envelope recipients
        self.read_recipients_from_headers = False  # -t
        self.ignore_dots = False               # -i / -oi
        self.from_addr: str | None = None      # -f / --from
        self.config_path: str | None = None
        self.dry_run = False
        self.verbose = False
        self.save_to_sent: bool | None = None
        self.verify = False                    # -bv: only show resolution
        self.noop_mode: str | None = None      # -bi / -bp
        # CLI mode fields
        self.to: list[str] = []
        self.cc: list[str] = []
        self.bcc: list[str] = []
        self.reply_to: list[str] = []
        self.subject: str | None = None
        self.body: str | None = None
        self.body_file: str | None = None
        self.html = False
        self.attachments: list[str] = []


USAGE = f"""\
usage (sendmail mode):
  {PROG} [-t] [-i] [-f from] [sendmail options...] [recipient...] < message
  {PROG} -bv recipient...      show how (local) addresses resolve

usage (CLI mode):
  {PROG} --to ADDR [--to ADDR ...] [--cc ADDR] [--bcc ADDR]
                   [--subject TEXT] [--body TEXT | --body-file FILE]
                   [--html] [--attach FILE ...] [--reply-to ADDR]
                   [--from ADDR]

common options:
  --config FILE          use this config file
  --save-to-sent / --no-save-to-sent
                         keep (or don't keep) a copy in the Sent Items folder
  --dry-run              print the Graph API payload instead of sending
  --verbose              debug output on stderr
  --version              print version and exit
  --help                 this help

Configuration is read from ~/.config/msgraph-sendmail.conf or
/etc/msgraph-sendmail.conf (see the example file shipped with the
project).  Full documentation: https://github.com/maciejczub/msgraph-sendmail
"""

# Long options that take a value.
_LONG_WITH_VALUE = {
    "--to", "--cc", "--bcc", "--reply-to", "--subject", "--body",
    "--body-file", "--attach", "--from", "--config",
}
_LONG_FLAGS = {
    "--html", "--dry-run", "--verbose", "--save-to-sent",
    "--no-save-to-sent", "--help", "--version", "--read-recipients",
}
# Any of these switches the tool into CLI (message-builder) mode.
_CLI_MODE_OPTIONS = {
    "--to", "--cc", "--bcc", "--subject", "--body", "--body-file",
    "--html", "--attach", "--reply-to",
}

# Classic sendmail single-letter options that take a value as the next
# argument when none is attached ("-F name" or "-Fname").  We honour -f
# and -r; the rest are accepted and ignored for compatibility.
_SENDMAIL_VALUE_OPTS = set("FBNRVXhCO")
# These only ever take an attached value ("-q30m", "-d0.1") — they must
# never swallow the next argument, which could be a recipient.
_SENDMAIL_ATTACHED_OPTS = set("qd")


def _split_addr_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_args(argv: list[str]) -> Options:
    opts = Options()
    i = 0
    n = len(argv)

    while i < n:
        arg = argv[i]

        if arg == "--":
            opts.recipients.extend(argv[i + 1:])
            break

        if arg.startswith("--"):
            name, eq, inline_value = arg.partition("=")
            if name in _LONG_WITH_VALUE:
                if eq:
                    value = inline_value
                else:
                    i += 1
                    if i >= n:
                        raise UserError(f"option {name} requires a value",
                                        EX_USAGE)
                    value = argv[i]
                _apply_long_value(opts, name, value)
            elif name in _LONG_FLAGS:
                _apply_long_flag(opts, name)
            else:
                raise UserError(f"unknown option {name}", EX_USAGE)
            if name in _CLI_MODE_OPTIONS:
                opts.mode = "cli"

        elif arg.startswith("-") and arg != "-":
            i = _parse_sendmail_option(opts, argv, i)

        else:
            opts.recipients.append(arg)

        i += 1

    if opts.mode == "cli" and (opts.read_recipients_from_headers
                               or opts.recipients or opts.verify):
        raise UserError(
            "cannot mix CLI options (--to/--subject/...) with sendmail-style "
            "recipients or -t", EX_USAGE)

    return opts


def _apply_long_value(opts: Options, name: str, value: str) -> None:
    if name == "--to":
        opts.to.extend(_split_addr_list(value))
    elif name == "--cc":
        opts.cc.extend(_split_addr_list(value))
    elif name == "--bcc":
        opts.bcc.extend(_split_addr_list(value))
    elif name == "--reply-to":
        opts.reply_to.extend(_split_addr_list(value))
    elif name == "--subject":
        opts.subject = value
    elif name == "--body":
        opts.body = value
    elif name == "--body-file":
        opts.body_file = value
    elif name == "--attach":
        opts.attachments.append(value)
    elif name == "--from":
        opts.from_addr = value
    elif name == "--config":
        opts.config_path = value


def _apply_long_flag(opts: Options, name: str) -> None:
    if name == "--html":
        opts.html = True
    elif name == "--dry-run":
        opts.dry_run = True
    elif name == "--verbose":
        opts.verbose = True
    elif name == "--save-to-sent":
        opts.save_to_sent = True
    elif name == "--no-save-to-sent":
        opts.save_to_sent = False
    elif name == "--read-recipients":
        opts.read_recipients_from_headers = True
    elif name == "--help":
        print(USAGE)
        raise SystemExit(EX_OK)
    elif name == "--version":
        print(f"{PROG} {__version__}")
        raise SystemExit(EX_OK)


def _parse_sendmail_option(opts: Options, argv: list[str], i: int) -> int:
    """Parse one classic sendmail-style option, returning the new index.

    Unknown options are ignored (with a debug note) so that whatever an
    application passes to its "sendmail binary" does not break delivery.
    """
    arg = argv[i]
    letter = arg[1]
    rest = arg[2:]

    if letter == "t":
        opts.read_recipients_from_headers = True
    elif letter == "i":
        opts.ignore_dots = True
    elif letter == "o":
        if rest == "i":  # -oi
            opts.ignore_dots = True
        # every other -oXXX processing option is a no-op for us
    elif letter in ("f", "r"):
        if rest:
            opts.from_addr = rest
        else:
            i += 1
            if i >= len(argv):
                raise UserError(f"option -{letter} requires an address",
                                EX_USAGE)
            opts.from_addr = argv[i]
    elif letter == "v":
        opts.verbose = True
    elif letter == "b":
        # -bX / -b X: operating mode.  Only -bv (verify) is special.
        mode = rest
        if not mode:
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                raise UserError("option -b requires a mode (e.g. -bv)",
                                EX_USAGE)
            i += 1
            mode = argv[i]
        if mode == "v":
            opts.verify = True
        elif mode in ("i", "p"):
            opts.noop_mode = mode   # newaliases / mailq
        elif mode != "m":
            raise UserError(
                f"sendmail mode -b{mode} is not supported; submit the "
                f"message on stdin with -t or recipient arguments", EX_USAGE)
    elif letter in _SENDMAIL_VALUE_OPTS:
        # Takes a value; swallow the next argument when none is attached.
        if not rest and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            i += 1
        debug(f"ignoring sendmail option {arg}")
    elif letter in _SENDMAIL_ATTACHED_OPTS:
        debug(f"ignoring sendmail option {arg}")
    else:
        # -bm, -G, -m, -n, -U, -Ac, -Am, ... — accept and ignore.
        debug(f"ignoring sendmail option {arg}")

    return i


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

def _recipient(address: str, name: str | None = None) -> dict:
    entry: dict = {"emailAddress": {"address": address}}
    if name:
        entry["emailAddress"]["name"] = name
    return entry


def _parse_addresses(values: list[str]) -> list[dict]:
    """Parse RFC 5322 address lists into Graph recipient objects."""
    result = []
    for name, address in email.utils.getaddresses(values):
        if address:
            result.append(_recipient(address, name or None))
    return result


def _dedupe(recipients: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for entry in recipients:
        key = entry["emailAddress"]["address"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _strip_lone_dot(data: bytes) -> bytes:
    """Emulate sendmail's end-of-message dot (input ends at a lone '.')."""
    lines = data.split(b"\n")
    for idx, line in enumerate(lines):
        if line.rstrip(b"\r") == b".":
            return b"\n".join(lines[:idx]) + b"\n"
    return data


def read_stdin_message(opts: Options) -> email.message.EmailMessage:
    data = sys.stdin.buffer.read()
    if not data.strip():
        raise UserError("empty message on stdin", EX_DATAERR)
    if not opts.ignore_dots:
        data = _strip_lone_dot(data)
    try:
        return email.message_from_bytes(data, policy=email.policy.default)
    except Exception as exc:
        raise UserError(f"cannot parse message from stdin: {exc}", EX_DATAERR)


def _attachment_from_part(part: email.message.EmailMessage) -> dict:
    payload = part.get_payload(decode=True) or b""
    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": part.get_filename() or "attachment",
        "contentType": part.get_content_type(),
        "contentBytes": base64.b64encode(payload).decode("ascii"),
    }
    content_id = part.get("Content-ID")
    if content_id:
        attachment["contentId"] = content_id.strip("<>")
        if part.get_content_disposition() != "attachment":
            attachment["isInline"] = True
    return attachment


def message_from_mime(msg: email.message.EmailMessage,
                      opts: Options) -> tuple[dict, str | None]:
    """Convert a parsed MIME message into a Graph message object.

    Returns (graph_message, from_header_address).
    """
    graph: dict = {}

    graph["subject"] = str(msg.get("Subject", ""))

    # Body: prefer the HTML alternative when present.
    body_part = msg.get_body(preferencelist=("html", "plain"))
    if body_part is not None:
        content = body_part.get_content()
        content_type = ("HTML" if body_part.get_content_type() == "text/html"
                        else "Text")
    elif not msg.is_multipart():
        content = msg.get_content() if msg.get_payload() is not None else ""
        content_type = "Text"
    else:
        content = ""
        content_type = "Text"
    graph["body"] = {"contentType": content_type, "content": content}

    # Recipients.
    to_recipients: list[dict] = []
    cc_recipients: list[dict] = []
    bcc_recipients: list[dict] = []

    if opts.read_recipients_from_headers:
        to_recipients.extend(_parse_addresses(msg.get_all("To", [])))
        cc_recipients.extend(_parse_addresses(msg.get_all("Cc", [])))
        bcc_recipients.extend(_parse_addresses(msg.get_all("Bcc", [])))

    to_recipients.extend(_parse_addresses(opts.recipients))

    graph["toRecipients"] = _dedupe(to_recipients)
    if cc_recipients:
        graph["ccRecipients"] = _dedupe(cc_recipients)
    if bcc_recipients:
        graph["bccRecipients"] = _dedupe(bcc_recipients)

    reply_to = _parse_addresses(msg.get_all("Reply-To", []))
    if reply_to:
        graph["replyTo"] = reply_to

    # Attachments (regular and inline).
    attachments = [
        _attachment_from_part(part) for part in msg.iter_attachments()
    ] if msg.is_multipart() else []
    if attachments:
        graph["attachments"] = attachments

    # Custom headers: Graph only accepts headers starting with "X-".
    custom_headers = []
    for name, value in msg.items():
        if name.lower().startswith("x-"):
            custom_headers.append({"name": name, "value": str(value)})
    if custom_headers:
        graph["internetMessageHeaders"] = custom_headers

    from_header = None
    parsed_from = _parse_addresses(msg.get_all("From", []))
    if parsed_from:
        from_header = parsed_from[0]["emailAddress"]["address"]

    return graph, from_header


def _attachment_from_file(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError as exc:
        raise UserError(f"cannot read attachment {path}: {exc}", EX_DATAERR)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": os.path.basename(path),
        "contentType": content_type,
        "contentBytes": base64.b64encode(payload).decode("ascii"),
    }


def message_from_cli(opts: Options) -> dict:
    if not opts.to:
        raise UserError("CLI mode requires at least one --to recipient",
                        EX_USAGE)

    if opts.body is not None:
        content = opts.body
    elif opts.body_file:
        try:
            with open(opts.body_file, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            raise UserError(f"cannot read body file: {exc}", EX_DATAERR)
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        content = ""

    graph: dict = {
        "subject": opts.subject or "",
        "body": {
            "contentType": "HTML" if opts.html else "Text",
            "content": content,
        },
        "toRecipients": _dedupe(_parse_addresses(opts.to)),
    }
    if opts.cc:
        graph["ccRecipients"] = _dedupe(_parse_addresses(opts.cc))
    if opts.bcc:
        graph["bccRecipients"] = _dedupe(_parse_addresses(opts.bcc))
    if opts.reply_to:
        graph["replyTo"] = _parse_addresses(opts.reply_to)
    if opts.attachments:
        graph["attachments"] = [
            _attachment_from_file(path) for path in opts.attachments
        ]
    return graph


# ---------------------------------------------------------------------------
# Local addresses: aliases, local_recipient fallback
# ---------------------------------------------------------------------------

def _split_alias_targets(value: str) -> list[str]:
    """Split an alias value into targets on commas, honouring double quotes
    (with backslash escapes) and RFC 5322 comments in parentheses.

    Pipe, file and :include: targets are returned verbatim (unquoted);
    everything else is reduced to a bare address with comments removed.
    Unparseable targets are dropped.
    """
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    depth = 0
    for char in value:
        if escaped:
            escaped = False
        elif char == "\\" and (quoted or depth):
            escaped = True
        elif char == '"' and not depth:
            quoted = not quoted
        elif char == "(" and not quoted:
            depth += 1
        elif char == ")" and not quoted and depth:
            depth -= 1
        elif char == "," and not quoted and not depth:
            tokens.append("".join(current))
            current = []
            continue
        current.append(char)
    tokens.append("".join(current))

    targets = []
    for token in (t.strip() for t in tokens):
        if not token:
            continue
        if len(token) > 1 and token[0] == token[-1] == '"' \
                and token[1:2] in ("|", "/", ":"):
            token = token[1:-1]
        if token.startswith(("|", "/", ":include:")):
            targets.append(token)
            continue
        words = token.split()
        if len(words) > 1 and not any(c in token for c in '<"(') \
                and all(_is_complete_address(word) for word in words):
            # "a@x b@y": bare addresses separated only by whitespace
            # (typically a continuation line without a trailing comma).
            parts = words
        else:
            parts = [token]  # e.g. a mailbox folded as "ops@" / "x.org"
        for part in parts:
            _, address = email.utils.parseaddr(part)
            if address:
                targets.append(address)
            else:
                debug("aliases: ignoring unparseable target")
    return targets


def _is_complete_address(word: str) -> bool:
    """A bare local name, or user@domain with both halves present."""
    if "@" not in word:
        return True
    local, _, domain = word.rpartition("@")
    return bool(local) and bool(domain) and not local.endswith("@")


def parse_aliases(text: str) -> dict[str, list[str]]:
    """Parse aliases(5) syntax: ``name: target, target`` with continuation
    lines (leading whitespace) and ``#`` comments.  Names are lower-cased;
    the first definition of a name wins, as with newaliases."""
    entries: list[tuple[str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if entries:
                name, value = entries[-1]
                entries[-1] = (name, value + " " + line.strip())
            continue
        name, colon, value = line.partition(":")
        name = name.strip().strip('"').lower()
        if not colon or not name:
            # Never echo the line itself: it may come from a file that
            # holds secrets (e.g. a misconfigured aliases_file).
            debug(f"aliases: ignoring malformed line {lineno}")
            continue
        entries.append((name, value))

    aliases: dict[str, list[str]] = {}
    for name, value in entries:
        if name in aliases:
            debug(f"aliases: duplicate entry for {name!r} ignored")
            continue
        aliases[name] = _split_alias_targets(value)
    return aliases


def _norm_domain(domain: str) -> str:
    """Lower-case and drop a trailing root dot.  Deliberately no IDNA
    mapping: IDNA 2003 folds distinct names (faß -> fass), list both the
    Unicode and the xn-- form in local_domains if you need them."""
    return domain.strip().lower().rstrip(".")


def _read_aliases_file(path: str) -> str | None:
    """Read a system aliases file defensively; None if it does not exist.

    The file decides where mail for root goes, so it must be a regular
    file owned by root or the invoking user and not writable by others;
    it is opened non-blocking (a FIFO cannot hang us) and size-capped.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserError(f"cannot read aliases file {path}: {exc}", EX_CONFIG)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UserError(f"aliases file {path} is not a regular file",
                            EX_CONFIG)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
                or st.st_uid not in (0, os.geteuid()):
            raise UserError(
                f"refusing to use aliases file {path}: it must be owned by "
                f"root (or you) and not writable by group/others", EX_CONFIG)
        if st.st_size > MAX_ALIASES_BYTES:
            raise UserError(f"aliases file {path} is larger than "
                            f"{MAX_ALIASES_BYTES // 1024} KiB", EX_CONFIG)
        chunks = []
        remaining = MAX_ALIASES_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            raise UserError(f"aliases file {path} is larger than "
                            f"{MAX_ALIASES_BYTES // 1024} KiB", EX_CONFIG)
    except OSError as exc:
        raise UserError(f"cannot read aliases file {path}: {exc}", EX_CONFIG)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "replace")


class AddressResolver:
    """Map local addresses (``root``, ``root@localhost``, ``www-data@host``)
    to real mailboxes the way an MTA would: [aliases] from the config, then
    the system aliases file, then the ``local_recipient`` catch-all.
    Addresses that are not local pass through unchanged."""

    MAX_DEPTH = 100
    MAX_EXPANSION_STEPS = 1_000_000  # alias targets visited per message

    def __init__(self, cfg: Config, hostname: str | None = None,
                 getfqdn=None) -> None:
        self.cfg = cfg
        self._hostname = _norm_domain(hostname if hostname is not None
                                      else socket.gethostname())
        self._getfqdn = getfqdn or socket.getfqdn
        self._fqdn: str | None = None
        self._system_aliases: dict[str, list[str]] | None = None
        self._cache: dict[str, list[str] | None] = {}  # per top-level name
        self._pure: dict[str, list[str] | None] = {}   # see _walk()
        self._steps = 0
        self.rewritten: list[str] = []   # original local addresses
        self.discarded: list[str] = []   # local addresses aliased to /dev/null

    # -- what is local -----------------------------------------------------

    def _fqdn_value(self) -> str:
        if self._fqdn is None:
            try:
                self._fqdn = _norm_domain(self._getfqdn() or "")
            except OSError:
                self._fqdn = ""
        return self._fqdn

    def _is_local_domain(self, domain: str) -> bool:
        domain = _norm_domain(domain)
        if self.cfg.local_domains is not None:
            return domain in {_norm_domain(d) for d in self.cfg.local_domains}
        if self.cfg.default_from:
            _, sender = email.utils.parseaddr(self.cfg.default_from)
            if "@" in sender and \
                    domain == _norm_domain(sender.rpartition("@")[2]):
                return False  # the real mail domain is never "local"
        short = self._hostname.split(".")[0]
        if domain in ("localhost", "localhost.localdomain", short,
                      self._hostname):
            return True
        # Only pay for a (possibly DNS-backed) FQDN lookup when the domain
        # could plausibly be this host's FQDN.
        if short and domain.startswith(short + "."):
            return domain == self._fqdn_value()
        return False

    def is_local(self, address: str) -> bool:
        _, at, domain = address.rpartition("@")
        if not at:
            return True
        return self._is_local_domain(domain)

    # -- alias lookup ------------------------------------------------------

    def _system(self) -> dict[str, list[str]]:
        if self._system_aliases is None:
            self._system_aliases = {}
            if self.cfg.aliases_file:
                text = _read_aliases_file(self.cfg.aliases_file)
                if text is not None:
                    self._system_aliases = parse_aliases(text)
        return self._system_aliases

    def _lookup(self, name: str) -> list[str] | None:
        if name in self.cfg.aliases:
            return self.cfg.aliases[name]
        return self._system().get(name)

    def _fallback(self) -> list[str] | None:
        catch_all = self.cfg.local_recipient
        if not catch_all:
            return None
        if self.is_local(catch_all):
            raise UserError(
                f"local_recipient {catch_all!r} is itself a local address; "
                f"it must be a real mailbox", EX_CONFIG)
        return [catch_all]

    def _walk(self, name: str, depth: int, out: list[str], seen: set[str],
              path: dict[str, int], done: set[str]) -> tuple[bool, int]:
        """Depth-first expansion of one local name into *out*.

        *path* maps the names on the current path to their depth; *done*
        holds names already expanded into *out* during this resolve().
        Returns (deliverable, low): *low* is the smallest path depth this
        subtree referred back to.  A subtree that never refers above its
        own root (low >= depth) has a context-independent result, which is
        cached and reused — so a big list behind many aliases is expanded
        once per message, and cyclic results never depend on lookup order.
        """
        if name in self._pure:
            return self._add(self._pure[name], out, seen), depth
        if depth >= self.MAX_DEPTH:
            raise UserError(
                f"alias nesting deeper than {self.MAX_DEPTH} levels "
                f"(at {name!r}); check the aliases", EX_CONFIG)
        path[name] = depth
        low = depth + 1  # "no back reference"
        mine: list[str] = []
        mine_seen: set[str] = set()
        targets = self._lookup(name)
        if targets is None:
            ok = self._add(self._fallback(), mine, mine_seen)
        else:
            ok = True
            usable = False
            for target in targets:
                self._spend()
                if target == "/dev/null":
                    usable = True  # explicit discard
                    continue
                if target.startswith(("|", "/", ":include:")):
                    debug(f"alias {name}: skipping undeliverable target "
                          f"{target!r}")
                    continue
                usable = True
                if not self.is_local(target):
                    self._add([target], mine, mine_seen)
                    continue
                target_name = (target.rpartition("@")[0] or target).lower()
                if target_name in path:
                    # Self reference / loop: "the local mailbox".
                    low = min(low, path[target_name])
                    ok = self._add(self._fallback(), mine, mine_seen)
                elif target_name in self._pure or target_name not in done:
                    ok, child_low = self._walk(target_name, depth + 1, mine,
                                               mine_seen, path, done)
                    low = min(low, child_low)
                else:
                    # Expanded earlier in this resolve() but context-
                    # dependent: its addresses are already in *out*, yet
                    # *mine* lacks them, so this result must not be cached.
                    low = -1
                if not ok:
                    break
            ok = ok and usable
        del path[name]
        done.add(name)
        if low >= depth:
            self._pure[name] = mine if ok else None
        if ok:
            self._add(mine, out, seen)
        return ok, low

    def _spend(self) -> None:
        self._steps += 1
        if self._steps > self.MAX_EXPANSION_STEPS:
            raise UserError(
                f"alias expansion exceeds {self.MAX_EXPANSION_STEPS} steps "
                f"for this message; check the aliases", EX_CONFIG)

    @staticmethod
    def _add(addresses: list[str] | None, out: list[str],
             seen: set[str]) -> bool:
        if addresses is None:
            return False
        for address in addresses:
            if address.lower() not in seen:
                seen.add(address.lower())
                out.append(address)
        return True

    def resolve(self, address: str) -> list[str] | None:
        """Return the real addresses for *address* ([] when it is aliased
        to /dev/null), or None when it is a local address that cannot be
        delivered."""
        if not self.is_local(address):
            return [address]
        name = (address.rpartition("@")[0] or address).lower()
        if name not in self._cache:
            out: list[str] = []
            ok, _ = self._walk(name, 0, out, set(), {}, set())
            self._cache[name] = out if ok else None
            debug(f"local address {address} -> "
                  + ("(undeliverable)" if not ok
                     else ", ".join(out[:10]) or "(discarded)")
                  + (" ..." if ok and len(out) > 10 else ""))
        result = self._cache[name]
        return None if result is None else list(result)

    def resolve_recipients(self, entries: list[dict],
                           reply_to: bool = False) -> list[dict]:
        """Resolve Graph recipient objects; raise EX_NOUSER for local
        addresses that cannot be resolved.  For *reply_to* lists,
        unresolvable addresses are dropped instead and nothing is counted
        as rewritten or discarded (Reply-To is not a delivery)."""
        result: list[dict] = []
        seen: set[str] = set()   # dedupe before building recipient objects
        unresolved: list[str] = []

        def add(entry: dict) -> None:
            key = entry["emailAddress"]["address"].lower()
            if key not in seen:
                seen.add(key)
                result.append(entry)

        for entry in entries:
            address = entry["emailAddress"]["address"]
            if not self.is_local(address):
                add(entry)
                continue
            resolved = self.resolve(address)
            if resolved is None:
                if reply_to:
                    debug(f"dropping unresolvable local address {address}")
                else:
                    unresolved.append(address)
                continue
            if not reply_to:
                (self.rewritten if resolved else self.discarded).append(
                    address)
            for target in resolved:
                if target.lower() not in seen:
                    add(_recipient(target))
        if unresolved:
            raise UserError(
                "no mailbox for local address(es) "
                + ", ".join(repr(a) for a in unresolved)
                + ": add them to the [aliases] section or the aliases file, "
                  "or set local_recipient in the config", EX_NOUSER)
        return result


# ---------------------------------------------------------------------------
# Microsoft Graph HTTP client
# ---------------------------------------------------------------------------

def _http_request(url: str, data: bytes, headers: dict,
                  timeout: float) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except (urllib.error.URLError, OSError) as exc:
        raise UserError(f"network error talking to {urllib.parse.urlsplit(url).netloc}: "
                        f"{getattr(exc, 'reason', exc)}", EX_TEMPFAIL)


def get_token(cfg: Config) -> str:
    url = f"{LOGIN_BASE}/{urllib.parse.quote(cfg.tenant)}/oauth2/v2.0/token"
    form = urllib.parse.urlencode({
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }).encode("ascii")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": f"{PROG}/{__version__}",
    }
    status, body, _ = _http_request(url, form, headers, cfg.timeout)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    if status != 200 or "access_token" not in payload:
        description = payload.get("error_description") or body[:300]
        raise UserError(
            f"authentication failed (HTTP {status}): {description}",
            EX_UNAVAILABLE)
    debug("obtained access token")
    return payload["access_token"]


def _graph_error_details(body: bytes) -> tuple[str, str]:
    try:
        error = json.loads(body).get("error", {})
        return error.get("code", ""), error.get("message", "")
    except ValueError:
        return "", body.decode("utf-8", "replace")[:300]


def send_message(cfg: Config, token: str, mailbox: str, graph_message: dict,
                 save_to_sent: bool) -> None:
    url = (f"{GRAPH_BASE}/users/{urllib.parse.quote(mailbox)}/sendMail")
    payload = json.dumps({
        "message": graph_message,
        "saveToSentItems": save_to_sent,
    }).encode("utf-8")

    if len(payload) > MAX_REQUEST_BYTES:
        raise UserError(
            f"message is {len(payload) // 1024} KiB after encoding; "
            f"Microsoft Graph sendMail requests are limited to 4 MiB "
            f"(large attachments would need an upload session, which is "
            f"not supported)", EX_DATAERR)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": f"{PROG}/{__version__}",
    }

    attempts = 4
    delay = 2.0
    for attempt in range(1, attempts + 1):
        status, body, response_headers = _http_request(
            url, payload, headers, cfg.timeout)

        if status == 202:
            debug(f"Graph accepted the message (attempt {attempt})")
            return

        code, message = _graph_error_details(body)
        retryable = status == 429 or 500 <= status < 600

        if retryable and attempt < attempts:
            retry_after = response_headers.get("Retry-After")
            try:
                wait = max(float(retry_after), delay) if retry_after else delay
            except ValueError:
                wait = delay
            debug(f"HTTP {status} ({code}); retrying in {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
            continue

        detail = f"HTTP {status} {code}: {message}".strip()
        if status in (401, 403):
            raise UserError(
                f"Graph rejected the request — check the app's Mail.Send "
                f"permission and admin consent ({detail})", EX_UNAVAILABLE)
        if status == 404:
            raise UserError(
                f"mailbox {mailbox!r} not found or has no license ({detail})",
                EX_UNAVAILABLE)
        if retryable:
            raise UserError(f"Graph unavailable after {attempts} attempts "
                            f"({detail})", EX_TEMPFAIL)
        raise UserError(f"Graph rejected the message ({detail})", EX_DATAERR)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _extract_bare_address(value: str) -> str:
    _, address = email.utils.parseaddr(value)
    return address or value


def verify_addresses(resolver: AddressResolver, recipients: list[str]) -> int:
    """sendmail -bv: report how each address resolves; send nothing."""
    addresses = [address for _, address in
                 email.utils.getaddresses(recipients) if address]
    if not addresses:
        raise UserError("-bv needs at least one address", EX_USAGE)
    status = EX_OK
    for address in addresses:
        resolved = resolver.resolve(address)
        if resolved is None:
            print(f"{address}... User unknown (no alias and no "
                  f"local_recipient)")
            status = EX_NOUSER
        elif not resolved:
            print(f"{address}... deliverable: discarded (/dev/null)")
        else:
            print(f"{address}... deliverable: {', '.join(resolved)}")
    return status


def run(argv: list[str]) -> int:
    global _VERBOSE

    opts = parse_args(argv)
    _VERBOSE = opts.verbose

    if opts.noop_mode == "i":
        debug("-bi (newaliases): nothing to do, aliases are read live")
        return EX_OK
    if opts.noop_mode == "p":
        print("Mail queue is empty (msgraph-sendmail does not queue)")
        return EX_OK

    cfg = load_config(opts.config_path)
    resolver = AddressResolver(cfg)

    if opts.verify:
        return verify_addresses(resolver, opts.recipients)

    if opts.mode == "cli":
        graph_message = message_from_cli(opts)
        from_header = None
    else:
        if not opts.recipients and not opts.read_recipients_from_headers:
            raise UserError(
                "no recipients: pass addresses as arguments or use -t "
                "(see --help)", EX_USAGE)
        mime_message = read_stdin_message(opts)
        graph_message, from_header = message_from_mime(mime_message, opts)

    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        if graph_message.get(key):
            graph_message[key] = resolver.resolve_recipients(
                graph_message[key])
    if graph_message.get("replyTo"):
        graph_message["replyTo"] = resolver.resolve_recipients(
            graph_message["replyTo"], reply_to=True)
        if not graph_message["replyTo"]:
            del graph_message["replyTo"]

    if resolver.discarded and not any(
            graph_message.get(key) for key in
            ("toRecipients", "ccRecipients", "bccRecipients")):
        log_info("discarded: all recipients aliased to /dev/null ("
                 + ", ".join(resolver.discarded) + ")")
        return EX_OK

    if not graph_message.get("toRecipients") \
            and not graph_message.get("ccRecipients") \
            and not graph_message.get("bccRecipients"):
        raise UserError("message has no recipients", EX_DATAERR)

    # Which mailbox do we send from?  -f/--from wins, then the From:
    # header, then the configured default.  Local addresses (root,
    # www-data@host, ...) can never be a Graph mailbox and are skipped.
    # A local -f (cron, daemons) means "system mail": go straight to the
    # configured mailbox rather than trusting the From: header.
    mailbox = None
    explicit = _extract_bare_address(opts.from_addr) if opts.from_addr \
        else None
    if explicit and resolver.is_local(explicit):
        debug(f"sender {explicit} is a local address; using configured from")
        candidates = (cfg.default_from,)
    else:
        candidates = (explicit, from_header, cfg.default_from)
    for candidate in candidates:
        if not candidate:
            continue
        if resolver.is_local(candidate):
            debug(f"sender {candidate} is a local address; skipping it")
            continue
        mailbox = candidate
        break
    if not mailbox:
        raise UserError(
            "no sender address: use -f/--from, a From: header, or set "
            "'from' in the config file", EX_CONFIG)

    save_to_sent = (opts.save_to_sent if opts.save_to_sent is not None
                    else cfg.save_to_sent)

    if opts.dry_run:
        print(json.dumps({
            "mailbox": mailbox,
            "saveToSentItems": save_to_sent,
            "message": graph_message,
        }, indent=2, ensure_ascii=False))
        return EX_OK

    require_config(cfg)
    token = get_token(cfg)
    send_message(cfg, token, mailbox, graph_message, save_to_sent)

    recipients_summary = ", ".join(
        entry["emailAddress"]["address"]
        for key in ("toRecipients", "ccRecipients", "bccRecipients")
        for entry in graph_message.get(key, [])
    )
    orig = (f" orig_to={', '.join(resolver.rewritten)}"
            if resolver.rewritten else "")
    log_info(f"sent from={mailbox} to={recipients_summary}{orig} "
             f"subject={graph_message.get('subject', '')!r}")
    debug("message accepted by Microsoft Graph")
    return EX_OK


def main() -> None:
    try:
        sys.exit(run(sys.argv[1:]))
    except UserError as exc:
        log_error(str(exc))
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.exit(EX_TEMPFAIL)


if __name__ == "__main__":
    main()
