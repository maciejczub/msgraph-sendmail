# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Local address handling (#1): mail to `root`, Unix users and
  `user@localhost`/`user@<hostname>` is resolved through a new `[aliases]`
  config section, the system aliases file (`aliases_file`, default
  `/etc/aliases`) and a `local_recipient` catch-all. New keys
  `local_recipient`, `aliases_file`, `local_domains`; environment
  overrides `MSGRAPH_SENDMAIL_LOCAL_RECIPIENT`,
  `MSGRAPH_SENDMAIL_ALIASES_FILE`.
- `-bv` / `-b v` (verify addresses) prints how recipients resolve without
  sending. `-bi` (newaliases) is a no-op because aliases are read live,
  `-bp` (mailq) reports an empty queue, `-bm` is the default; other modes
  (e.g. `-bs`, used by some PHP mailers) and a bare `-b` are usage errors
  instead of being silently ignored.
- Aliases to `/dev/null` discard mail; the aliases file must be a regular,
  root/user-owned, not group/world-writable file of at most 1 MiB.
- Syslog `sent` line includes `orig_to=` when local addresses were
  rewritten.

### Changed
- Config file errors (a stray `%`, syntax errors) are reported as exit 78
  with file, line and option names only — never the offending text, which
  could be the client secret. Previously a stray `%` crashed with a
  traceback; write `%%` for a literal percent sign, as before.
- `from` and `local_recipient` must be exactly one full address
  (`user@domain`, optionally `Name <user@domain>`); this is checked at
  start-up.
- Local sender addresses (`-f root`, `From: root (Cron Daemon)`) are
  skipped when choosing the sending mailbox; a local `-f` selects `from`
  from the config directly, a local `From:` header falls through to it.
- Unresolvable local recipients now fail with exit code 67 (`EX_NOUSER`)
  before anything is sent, instead of being rejected by Graph.

## [1.0.0] - 2026-07-06

### Added
- Initial release.
- Sendmail-compatible mode: RFC 822/MIME from stdin, `-t`, `-f`/`-r`,
  `-i`/`-oi`, lone-dot termination, tolerant handling of all other classic
  sendmail options.
- CLI mode: `--to/--cc/--bcc/--subject/--body/--body-file/--html/--attach/
  --reply-to`.
- Full MIME conversion to Graph messages: text and HTML bodies,
  attachments, inline images (Content-ID), display names, Reply-To,
  `X-*` header passthrough, UTF-8.
- OAuth 2.0 client credentials authentication against Microsoft Entra ID.
- Retry with exponential backoff and `Retry-After` support for HTTP 429
  and 5xx responses.
- sysexits-compatible exit codes and syslog logging (mail facility).
- INI configuration with environment-variable overrides and
  `client_secret_file` support; warns about world-readable secrets.
- `--dry-run` payload preview, `--verbose` debugging.
- Documentation: Entra ID setup guide (incl. application access policy),
  configuration reference, system integration guide (symlink,
  update-alternatives, PHP, cron, postfix bridge), man page.
