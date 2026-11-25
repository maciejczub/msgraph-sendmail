"""Unit tests for msgraph-sendmail (stdlib unittest, no external deps).

Run with:  python3 -m unittest discover -s tests -v
"""

import base64
import email
import email.policy
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import msgraph_sendmail as m  # noqa: E402


def parse_mime(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


class TestArgParsing(unittest.TestCase):
    def test_positional_recipients(self):
        opts = m.parse_args(["alice@example.com", "bob@example.com"])
        self.assertEqual(opts.mode, "sendmail")
        self.assertEqual(opts.recipients,
                         ["alice@example.com", "bob@example.com"])

    def test_dash_t(self):
        opts = m.parse_args(["-t"])
        self.assertTrue(opts.read_recipients_from_headers)

    def test_dash_f_attached_and_separate(self):
        self.assertEqual(m.parse_args(["-fwww@x.pl", "a@b"]).from_addr,
                         "www@x.pl")
        self.assertEqual(m.parse_args(["-f", "www@x.pl", "a@b"]).from_addr,
                         "www@x.pl")

    def test_dash_r_alias(self):
        self.assertEqual(m.parse_args(["-r", "www@x.pl", "a@b"]).from_addr,
                         "www@x.pl")

    def test_oi_and_i_set_ignore_dots(self):
        self.assertTrue(m.parse_args(["-oi", "a@b"]).ignore_dots)
        self.assertTrue(m.parse_args(["-i", "a@b"]).ignore_dots)

    def test_typical_cron_invocation(self):
        # Vixie cron calls: sendmail -i -FCronDaemon -odi -oem -oi -t
        opts = m.parse_args(["-i", "-FCronDaemon", "-odi", "-oem",
                             "-oi", "-t"])
        self.assertTrue(opts.ignore_dots)
        self.assertTrue(opts.read_recipients_from_headers)
        self.assertEqual(opts.recipients, [])

    def test_typical_php_invocation(self):
        opts = m.parse_args(["-t", "-i"])
        self.assertTrue(opts.read_recipients_from_headers)
        self.assertTrue(opts.ignore_dots)

    def test_ignored_value_option_does_not_eat_recipient(self):
        # -q takes only an attached value; "user@x" must stay a recipient.
        opts = m.parse_args(["-q30m", "user@x.example"])
        self.assertEqual(opts.recipients, ["user@x.example"])

    def test_F_separate_value_is_swallowed(self):
        opts = m.parse_args(["-F", "Cron Daemon", "-t"])
        self.assertEqual(opts.recipients, [])
        self.assertTrue(opts.read_recipients_from_headers)

    def test_cli_mode_detection(self):
        opts = m.parse_args(["--to", "a@b.example", "--subject", "Hi"])
        self.assertEqual(opts.mode, "cli")
        self.assertEqual(opts.to, ["a@b.example"])
        self.assertEqual(opts.subject, "Hi")

    def test_cli_comma_separated_recipients(self):
        opts = m.parse_args(["--to", "a@x.example, b@x.example"])
        self.assertEqual(opts.to, ["a@x.example", "b@x.example"])

    def test_long_option_with_equals(self):
        opts = m.parse_args(["--subject=Hello world", "--to=a@b.example"])
        self.assertEqual(opts.subject, "Hello world")

    def test_mixing_modes_is_an_error(self):
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["--to", "a@b.example", "-t"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def test_unknown_long_option_is_an_error(self):
        with self.assertRaises(m.UserError):
            m.parse_args(["--frobnicate"])

    def test_double_dash_ends_options(self):
        opts = m.parse_args(["-t", "--", "-weird@example.com"])
        self.assertEqual(opts.recipients, ["-weird@example.com"])


class TestLoneDot(unittest.TestCase):
    def test_message_truncated_at_lone_dot(self):
        data = b"Subject: x\n\nline1\n.\nsecret\n"
        self.assertEqual(m._strip_lone_dot(data),
                         b"Subject: x\n\nline1\n")

    def test_crlf_dot(self):
        data = b"Subject: x\r\n\r\nline1\r\n.\r\nsecret\r\n"
        self.assertNotIn(b"secret", m._strip_lone_dot(data))

    def test_no_dot_untouched(self):
        data = b"Subject: x\n\nbody\n"
        self.assertEqual(m._strip_lone_dot(data), data)

    def test_dot_in_text_not_a_terminator(self):
        data = b"Subject: x\n\nfoo.\n.bar\n"
        self.assertEqual(m._strip_lone_dot(data), data)


class TestMimeConversion(unittest.TestCase):
    def _convert(self, raw: bytes, args=None):
        opts = m.parse_args(args or ["-t"])
        return m.message_from_mime(parse_mime(raw), opts)

    def test_plain_text(self):
        graph, from_addr = self._convert(
            b"From: Sender <s@x.example>\n"
            b"To: r@x.example\n"
            b"Subject: Test\n"
            b"\n"
            b"Hello\n")
        self.assertEqual(graph["subject"], "Test")
        self.assertEqual(graph["body"]["contentType"], "Text")
        self.assertEqual(graph["body"]["content"].strip(), "Hello")
        self.assertEqual(graph["toRecipients"],
                         [{"emailAddress": {"address": "r@x.example"}}])
        self.assertEqual(from_addr, "s@x.example")

    def test_display_names_preserved(self):
        graph, _ = self._convert(
            b"To: Jan Kowalski <jan@x.example>\n"
            b"Subject: t\n\nx\n")
        self.assertEqual(graph["toRecipients"][0]["emailAddress"],
                         {"address": "jan@x.example", "name": "Jan Kowalski"})

    def test_multipart_alternative_prefers_html(self):
        raw = (
            b"To: r@x.example\n"
            b"Subject: alt\n"
            b"MIME-Version: 1.0\n"
            b"Content-Type: multipart/alternative; boundary=B\n"
            b"\n--B\n"
            b"Content-Type: text/plain\n\nplain text\n"
            b"\n--B\n"
            b"Content-Type: text/html\n\n<p>html text</p>\n"
            b"\n--B--\n")
        graph, _ = self._convert(raw)
        self.assertEqual(graph["body"]["contentType"], "HTML")
        self.assertIn("<p>html text</p>", graph["body"]["content"])

    def test_bcc_goes_to_bcc_recipients(self):
        graph, _ = self._convert(
            b"To: to@x.example\n"
            b"Cc: cc@x.example\n"
            b"Bcc: bcc@x.example\n"
            b"Subject: t\n\nx\n")
        self.assertEqual(graph["bccRecipients"][0]["emailAddress"]["address"],
                         "bcc@x.example")
        self.assertEqual(graph["ccRecipients"][0]["emailAddress"]["address"],
                         "cc@x.example")

    def test_cmdline_recipients_added_and_deduped(self):
        graph, _ = self._convert(
            b"To: a@x.example\nSubject: t\n\nx\n",
            args=["-t", "a@x.example", "b@x.example"])
        addresses = [r["emailAddress"]["address"]
                     for r in graph["toRecipients"]]
        self.assertEqual(addresses, ["a@x.example", "b@x.example"])

    def test_without_t_headers_are_ignored(self):
        graph, _ = self._convert(
            b"To: header@x.example\nSubject: t\n\nx\n",
            args=["arg@x.example"])
        addresses = [r["emailAddress"]["address"]
                     for r in graph["toRecipients"]]
        self.assertEqual(addresses, ["arg@x.example"])

    def test_attachment_extraction(self):
        raw = (
            b"To: r@x.example\n"
            b"Subject: att\n"
            b"MIME-Version: 1.0\n"
            b"Content-Type: multipart/mixed; boundary=B\n"
            b"\n--B\n"
            b"Content-Type: text/plain\n\nsee attachment\n"
            b"\n--B\n"
            b"Content-Type: application/pdf; name=doc.pdf\n"
            b"Content-Disposition: attachment; filename=doc.pdf\n"
            b"Content-Transfer-Encoding: base64\n"
            b"\n" + base64.b64encode(b"%PDF-fake") + b"\n"
            b"\n--B--\n")
        graph, _ = self._convert(raw)
        self.assertEqual(len(graph["attachments"]), 1)
        att = graph["attachments"][0]
        self.assertEqual(att["name"], "doc.pdf")
        self.assertEqual(att["contentType"], "application/pdf")
        self.assertEqual(base64.b64decode(att["contentBytes"]), b"%PDF-fake")

    def test_inline_image_gets_content_id(self):
        raw = (
            b"To: r@x.example\n"
            b"Subject: inline\n"
            b"MIME-Version: 1.0\n"
            b"Content-Type: multipart/related; boundary=B\n"
            b"\n--B\n"
            b"Content-Type: text/html\n\n<img src=\"cid:logo1\">\n"
            b"\n--B\n"
            b"Content-Type: image/png\n"
            b"Content-ID: <logo1>\n"
            b"Content-Transfer-Encoding: base64\n"
            b"\n" + base64.b64encode(b"PNG-fake") + b"\n"
            b"\n--B--\n")
        graph, _ = self._convert(raw)
        att = graph["attachments"][0]
        self.assertEqual(att["contentId"], "logo1")
        self.assertTrue(att["isInline"])

    def test_reply_to(self):
        graph, _ = self._convert(
            b"To: r@x.example\nReply-To: rt@x.example\nSubject: t\n\nx\n")
        self.assertEqual(graph["replyTo"][0]["emailAddress"]["address"],
                         "rt@x.example")

    def test_x_headers_forwarded_others_dropped(self):
        graph, _ = self._convert(
            b"To: r@x.example\n"
            b"Subject: t\n"
            b"X-Mailer: PHP/8.3\n"
            b"Precedence: bulk\n"
            b"\nx\n")
        headers = graph.get("internetMessageHeaders", [])
        names = [h["name"] for h in headers]
        self.assertIn("X-Mailer", names)
        self.assertNotIn("Precedence", names)

    def test_utf8_subject_and_body(self):
        raw = (
            "To: r@x.example\n"
            "Subject: Zażółć gęślą jaźń\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "\n"
            "Łódź żółta\n").encode("utf-8")
        graph, _ = self._convert(raw)
        self.assertEqual(graph["subject"], "Zażółć gęślą jaźń")
        self.assertIn("Łódź żółta", graph["body"]["content"])

    def test_headerless_body_with_cmdline_recipient(self):
        graph, from_addr = self._convert(
            b"just a body line\n", args=["r@x.example"])
        self.assertEqual(graph["toRecipients"][0]["emailAddress"]["address"],
                         "r@x.example")
        self.assertIsNone(from_addr)


class TestCliMessage(unittest.TestCase):
    def test_basic(self):
        opts = m.parse_args(["--to", "a@x.example", "--subject", "S",
                             "--body", "B"])
        graph = m.message_from_cli(opts)
        self.assertEqual(graph["subject"], "S")
        self.assertEqual(graph["body"], {"contentType": "Text",
                                         "content": "B"})

    def test_html_flag(self):
        opts = m.parse_args(["--to", "a@x.example", "--body", "<b>x</b>",
                             "--html"])
        graph = m.message_from_cli(opts)
        self.assertEqual(graph["body"]["contentType"], "HTML")

    def test_missing_to_is_usage_error(self):
        opts = m.parse_args(["--subject", "S", "--body", "B"])
        with self.assertRaises(m.UserError) as ctx:
            m.message_from_cli(opts)
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def test_attachment_from_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(b"file body")
            path = fh.name
        try:
            opts = m.parse_args(["--to", "a@x.example", "--body", "B",
                                 "--attach", path])
            graph = m.message_from_cli(opts)
            att = graph["attachments"][0]
            self.assertEqual(att["name"], os.path.basename(path))
            self.assertEqual(base64.b64decode(att["contentBytes"]),
                             b"file body")
            self.assertEqual(att["contentType"], "text/plain")
        finally:
            os.unlink(path)


class TestConfig(unittest.TestCase):
    def test_env_overrides_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write("[msgraph]\ntenant = file-tenant\n"
                     "client_id = file-id\nclient_secret = file-secret\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ,
                                 {"MSGRAPH_SENDMAIL_TENANT": "env-tenant"}):
                cfg = m.load_config(path)
            self.assertEqual(cfg.tenant, "env-tenant")
            self.assertEqual(cfg.client_id, "file-id")
        finally:
            os.unlink(path)

    def test_missing_required_raises_config_error(self):
        cfg = m.Config()
        with self.assertRaises(m.UserError) as ctx:
            m.require_config(cfg)
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)


class _FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestHttp(unittest.TestCase):
    def _cfg(self):
        cfg = m.Config()
        cfg.tenant = "t"
        cfg.client_id = "c"
        cfg.client_secret = "s"
        return cfg

    def test_send_success(self):
        with mock.patch.object(m.urllib.request, "urlopen",
                               return_value=_FakeResponse(202, b"")):
            m.send_message(self._cfg(), "tok", "s@x.example",
                           {"subject": "t", "toRecipients": []}, True)

    def test_retry_on_429_then_success(self):
        responses = [
            _FakeResponse(429, b'{"error":{"code":"TooManyRequests",'
                               b'"message":"slow down"}}',
                          {"Retry-After": "0"}),
            _FakeResponse(202, b""),
        ]
        with mock.patch.object(m.urllib.request, "urlopen",
                               side_effect=responses), \
             mock.patch.object(m.time, "sleep") as sleep:
            m.send_message(self._cfg(), "tok", "s@x.example",
                           {"subject": "t"}, True)
        self.assertTrue(sleep.called)

    def test_permanent_400_raises_dataerr(self):
        response = _FakeResponse(
            400, b'{"error":{"code":"ErrorInvalidRecipients",'
                 b'"message":"bad recipient"}}')
        with mock.patch.object(m.urllib.request, "urlopen",
                               return_value=response):
            with self.assertRaises(m.UserError) as ctx:
                m.send_message(self._cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True)
        self.assertEqual(ctx.exception.exit_code, m.EX_DATAERR)

    def test_403_raises_unavailable(self):
        response = _FakeResponse(
            403, b'{"error":{"code":"ErrorAccessDenied",'
                 b'"message":"denied"}}')
        with mock.patch.object(m.urllib.request, "urlopen",
                               return_value=response):
            with self.assertRaises(m.UserError) as ctx:
                m.send_message(self._cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True)
        self.assertEqual(ctx.exception.exit_code, m.EX_UNAVAILABLE)

    def test_oversized_message_rejected_locally(self):
        big = {"subject": "t",
               "body": {"contentType": "Text", "content": "x" * (5 << 20)}}
        with self.assertRaises(m.UserError) as ctx:
            m.send_message(self._cfg(), "tok", "s@x.example", big, True)
        self.assertEqual(ctx.exception.exit_code, m.EX_DATAERR)

    def test_token_error_reports_description(self):
        response = _FakeResponse(
            401, json.dumps({"error": "invalid_client",
                             "error_description": "AADSTS7000215"}).encode())
        with mock.patch.object(m.urllib.request, "urlopen",
                               return_value=response):
            with self.assertRaises(m.UserError) as ctx:
                m.get_token(self._cfg())
        self.assertIn("AADSTS7000215", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, m.EX_UNAVAILABLE)


class TestRunDryRun(unittest.TestCase):
    def test_sendmail_mode_dry_run(self):
        stdin_bytes = (b"From: s@x.example\nTo: r@x.example\n"
                       b"Subject: dry\n\nbody\n")
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(stdin_bytes)
        stdout = io.StringIO()
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m.sys, "stdout", stdout), \
             mock.patch.object(m, "load_config", return_value=m.Config()):
            code = m.run(["-t", "--dry-run"])
        self.assertEqual(code, m.EX_OK)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mailbox"], "s@x.example")
        self.assertEqual(payload["message"]["subject"], "dry")



# ---------------------------------------------------------------------------
# Local address handling (issue #1)
# ---------------------------------------------------------------------------

def _cfg(**kwargs):
    cfg = m.Config()
    cfg.aliases_file = ""          # never touch the real /etc/aliases
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def _resolver(cfg=None, hostname="web01", fqdn="web01.corp.example"):
    return m.AddressResolver(cfg or _cfg(), hostname=hostname,
                             getfqdn=lambda: fqdn)


class TestLocalDetection(unittest.TestCase):
    def test_unqualified_address_is_local(self):
        self.assertTrue(_resolver().is_local("root"))

    def test_localhost_domains_are_local(self):
        r = _resolver()
        self.assertTrue(r.is_local("root@localhost"))
        self.assertTrue(r.is_local("root@LOCALHOST.localdomain"))

    def test_short_hostname_and_fqdn_are_local(self):
        r = _resolver()
        self.assertTrue(r.is_local("root@web01"))
        self.assertTrue(r.is_local("root@Web01.Corp.Example"))

    def test_hostname_given_as_fqdn(self):
        r = _resolver(hostname="web01.corp.example", fqdn="web01.corp.example")
        self.assertTrue(r.is_local("root@web01"))
        self.assertTrue(r.is_local("root@web01.corp.example"))

    def test_external_address_is_not_local(self):
        self.assertFalse(_resolver().is_local("alice@contoso.com"))

    def test_fqdn_not_looked_up_for_unrelated_domains(self):
        lookups = []
        r = m.AddressResolver(_cfg(), hostname="web01",
                              getfqdn=lambda: lookups.append(1) or "x")
        r.is_local("alice@contoso.com")
        self.assertEqual(lookups, [])

    def test_sender_domain_is_never_local_by_default(self):
        cfg = _cfg(default_from="noreply@contoso.com")
        r = _resolver(cfg, hostname="contoso", fqdn="contoso.com")
        self.assertFalse(r.is_local("alice@contoso.com"))
        self.assertTrue(r.is_local("root@contoso"))

    def test_explicit_local_domains_replace_defaults(self):
        r = _resolver(_cfg(local_domains={"corp.lan"}))
        self.assertTrue(r.is_local("root@corp.lan"))
        self.assertFalse(r.is_local("root@localhost"))
        self.assertTrue(r.is_local("root"))


class TestAliasesParsing(unittest.TestCase):
    def test_basic_entries_comments_and_case(self):
        aliases = m.parse_aliases(
            "# comment\n\nPostmaster: root\nroot: a@x.example, b@x.example\n")
        self.assertEqual(aliases["postmaster"], ["root"])
        self.assertEqual(aliases["root"], ["a@x.example", "b@x.example"])

    def test_continuation_lines(self):
        aliases = m.parse_aliases("ops: a@x.example,\n  b@x.example\n"
                                  "\tc@x.example\n")
        self.assertEqual(aliases["ops"],
                         ["a@x.example", "b@x.example", "c@x.example"])

    def test_quoted_targets_keep_commas(self):
        aliases = m.parse_aliases('bot: "|/usr/bin/x a, b", a@x.example\n')
        self.assertEqual(aliases["bot"], ["|/usr/bin/x a, b", "a@x.example"])

    def test_first_definition_wins(self):
        aliases = m.parse_aliases("root: a@x.example\nroot: b@x.example\n")
        self.assertEqual(aliases["root"], ["a@x.example"])

    def test_lines_without_colon_are_ignored(self):
        self.assertEqual(m.parse_aliases("garbage line\n"), {})


class TestResolution(unittest.TestCase):
    def test_external_address_passes_through(self):
        self.assertEqual(_resolver().resolve("alice@contoso.com"),
                         ["alice@contoso.com"])

    def test_config_alias(self):
        r = _resolver(_cfg(aliases={"root": ["ops@contoso.com"]}))
        self.assertEqual(r.resolve("root@localhost"), ["ops@contoso.com"])

    def test_recursive_aliases(self):
        r = _resolver(_cfg(aliases={
            "postmaster": ["root"], "root": ["admin"],
            "admin": ["ops@contoso.com", "boss@contoso.com"]}))
        self.assertEqual(r.resolve("postmaster"),
                         ["ops@contoso.com", "boss@contoso.com"])

    def test_fallback_to_local_recipient(self):
        r = _resolver(_cfg(local_recipient="ops@contoso.com"))
        self.assertEqual(r.resolve("deploy"), ["ops@contoso.com"])

    def test_alias_to_unix_user_lands_on_local_recipient(self):
        r = _resolver(_cfg(aliases={"root": ["maciej"]},
                           local_recipient="ops@contoso.com"))
        self.assertEqual(r.resolve("root"), ["ops@contoso.com"])

    def test_self_reference_means_local_mailbox(self):
        r = _resolver(_cfg(aliases={"root": ["root", "a@contoso.com"]},
                           local_recipient="ops@contoso.com"))
        self.assertEqual(r.resolve("root"),
                         ["ops@contoso.com", "a@contoso.com"])

    def test_loop_terminates(self):
        r = _resolver(_cfg(aliases={"a": ["b"], "b": ["a"]},
                           local_recipient="ops@contoso.com"))
        self.assertEqual(r.resolve("a"), ["ops@contoso.com"])

    def test_unsupported_targets_skipped(self):
        r = _resolver(_cfg(aliases={
            "bot": ["|/bin/cat", "/var/mail/x", ":include:/etc/list",
                    "/dev/null",
                    "a@contoso.com"]}))
        self.assertEqual(r.resolve("bot"), ["a@contoso.com"])

    def test_only_unsupported_targets_is_unresolved(self):
        r = _resolver(_cfg(aliases={"bot": ["|/bin/cat"]}))
        self.assertIsNone(r.resolve("bot"))

    def test_unresolved_without_fallback_returns_none(self):
        self.assertIsNone(_resolver().resolve("deploy"))

    def test_config_aliases_override_aliases_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("root: file@contoso.com\nwww-data: web@contoso.com\n")
            path = fh.name
        try:
            os.chmod(path, 0o644)
            r = _resolver(_cfg(aliases_file=path,
                               aliases={"root": ["conf@contoso.com"]}))
            self.assertEqual(r.resolve("root"), ["conf@contoso.com"])
            self.assertEqual(r.resolve("www-data"), ["web@contoso.com"])
        finally:
            os.unlink(path)

    def test_missing_aliases_file_is_ignored(self):
        r = _resolver(_cfg(aliases_file="/nonexistent/aliases",
                           local_recipient="ops@contoso.com"))
        self.assertEqual(r.resolve("root"), ["ops@contoso.com"])

    def test_resolve_recipients_rewrites_and_dedupes(self):
        r = _resolver(_cfg(local_recipient="ops@contoso.com"))
        entries = [m._recipient("root", "Root"),
                   m._recipient("ops@contoso.com"),
                   m._recipient("alice@contoso.com", "Alice")]
        result = r.resolve_recipients(entries)
        self.assertEqual(
            [e["emailAddress"]["address"] for e in result],
            ["ops@contoso.com", "alice@contoso.com"])
        self.assertEqual(result[1]["emailAddress"]["name"], "Alice")
        self.assertEqual(r.rewritten, ["root"])

    def test_resolve_recipients_unresolved_raises_nouser(self):
        r = _resolver()
        with self.assertRaises(m.UserError) as ctx:
            r.resolve_recipients([m._recipient("deploy"),
                                  m._recipient("root@localhost")])
        self.assertEqual(ctx.exception.exit_code, m.EX_NOUSER)
        self.assertIn("deploy", str(ctx.exception))
        self.assertIn("root@localhost", str(ctx.exception))


class TestLocalConfig(unittest.TestCase):
    def _load(self, text, env=None):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            with mock.patch.dict(os.environ, env or {}, clear=False):
                return m.load_config(path)
        finally:
            os.unlink(path)

    def test_defaults(self):
        cfg = self._load("[msgraph]\n")
        self.assertEqual(cfg.aliases_file, "/etc/aliases")
        self.assertIsNone(cfg.local_domains)
        self.assertIsNone(cfg.local_recipient)
        self.assertEqual(cfg.aliases, {})

    def test_local_keys_and_aliases_section(self):
        cfg = self._load(
            "[msgraph]\nlocal_recipient = ops@contoso.com\n"
            "aliases_file =\nlocal_domains = corp.lan, Web01\n"
            "[aliases]\nRoot = ops@contoso.com, boss@contoso.com\n")
        self.assertEqual(cfg.local_recipient, "ops@contoso.com")
        self.assertEqual(cfg.aliases_file, "")
        self.assertEqual(cfg.local_domains, {"corp.lan", "web01"})
        self.assertEqual(cfg.aliases,
                         {"root": ["ops@contoso.com", "boss@contoso.com"]})

    def test_env_overrides(self):
        cfg = self._load("[msgraph]\nlocal_recipient = a@contoso.com\n", {
            "MSGRAPH_SENDMAIL_LOCAL_RECIPIENT": "b@contoso.com",
            "MSGRAPH_SENDMAIL_ALIASES_FILE": "/tmp/aliases"})
        self.assertEqual(cfg.local_recipient, "b@contoso.com")
        self.assertEqual(cfg.aliases_file, "/tmp/aliases")

    def test_unqualified_local_recipient_is_config_error(self):
        with self.assertRaises(m.UserError) as ctx:
            self._load("[msgraph]\nlocal_recipient = root\n")
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)


class TestRunLocal(unittest.TestCase):
    def _run(self, argv, stdin_bytes=b"", cfg=None):
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(stdin_bytes)
        stdout = io.StringIO()
        cfg = cfg or _cfg(default_from="noreply@contoso.com",
                          local_recipient="ops@contoso.com")
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m.sys, "stdout", stdout), \
             mock.patch.object(m, "load_config", return_value=cfg), \
             mock.patch.object(m.socket, "gethostname",
                               return_value="web01"), \
             mock.patch.object(m.socket, "getfqdn",
                               return_value="web01.corp.example"):
            code = m.run(argv)
        return code, stdout.getvalue()

    def test_debian_cron_invocation(self):
        code, out = self._run(
            ["-FCronDaemon", "-i", "-B8BITMIME", "-oem", "root", "--dry-run"],
            b"From: root (Cron Daemon)\nTo: root\nSubject: Cron <root@web01>"
            b" backup\n\noutput\n")
        self.assertEqual(code, m.EX_OK)
        payload = json.loads(out)
        self.assertEqual(payload["mailbox"], "noreply@contoso.com")
        self.assertEqual(payload["message"]["toRecipients"],
                         [m._recipient("ops@contoso.com")])

    def test_dash_t_resolves_header_recipients(self):
        code, out = self._run(
            ["-t", "--dry-run"],
            b"From: root@web01\nTo: root@localhost, alice@contoso.com\n"
            b"Cc: www-data\nReply-To: nobody\nSubject: s\n\nb\n",
            _cfg(default_from="noreply@contoso.com",
                 aliases={"www-data": ["web@contoso.com"]},
                 local_recipient="ops@contoso.com"))
        payload = json.loads(out)
        msg = payload["message"]
        self.assertEqual([e["emailAddress"]["address"]
                          for e in msg["toRecipients"]],
                         ["ops@contoso.com", "alice@contoso.com"])
        self.assertEqual(msg["ccRecipients"],
                         [m._recipient("web@contoso.com")])
        self.assertEqual(msg["replyTo"], [m._recipient("ops@contoso.com")])
        self.assertEqual(payload["mailbox"], "noreply@contoso.com")

    def test_unresolvable_reply_to_is_dropped(self):
        code, out = self._run(
            ["-t", "--dry-run"],
            b"To: alice@contoso.com\nReply-To: nobody\nSubject: s\n\nb\n",
            _cfg(default_from="noreply@contoso.com"))
        self.assertNotIn("replyTo", json.loads(out)["message"])

    def test_local_from_header_falls_through_to_config(self):
        code, out = self._run(
            ["alice@contoso.com", "--dry-run"],
            b"From: www-data@web01\nSubject: s\n\nb\n")
        self.assertEqual(json.loads(out)["mailbox"], "noreply@contoso.com")

    def test_all_senders_local_without_from_is_config_error(self):
        with self.assertRaises(m.UserError) as ctx:
            self._run(["alice@contoso.com", "--dry-run"],
                      b"From: root\nSubject: s\n\nb\n", _cfg())
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_unresolved_recipient_fails_before_sending(self):
        with self.assertRaises(m.UserError) as ctx:
            self._run(["deploy", "--dry-run"], b"Subject: s\n\nb\n",
                      _cfg(default_from="noreply@contoso.com"))
        self.assertEqual(ctx.exception.exit_code, m.EX_NOUSER)

    def test_cli_mode_resolves(self):
        code, out = self._run(["--to", "root", "--body", "x", "--dry-run"])
        self.assertEqual(json.loads(out)["message"]["toRecipients"],
                         [m._recipient("ops@contoso.com")])

    def test_bv_reports_resolution(self):
        code, out = self._run(
            ["-bv", "root", "alice@contoso.com"], b"",
            _cfg(aliases={"root": ["a@contoso.com", "b@contoso.com"]}))
        self.assertEqual(code, m.EX_OK)
        self.assertIn("root... deliverable: a@contoso.com, b@contoso.com",
                      out)
        self.assertIn("alice@contoso.com... deliverable: alice@contoso.com",
                      out)

    def test_bv_unknown_user_exit_code(self):
        code, out = self._run(["-bv", "deploy"], b"", _cfg())
        self.assertEqual(code, m.EX_NOUSER)
        self.assertIn("deploy... User unknown", out)

    def test_bv_without_recipients_is_usage_error(self):
        with self.assertRaises(m.UserError) as ctx:
            self._run(["-bv"], b"", _cfg())
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)



class TestReviewFindings(unittest.TestCase):
    """Regression tests for the issues found in the Codex review of #1."""

    def _aliases_file(self, text, mode=0o644):
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", delete=False)
        fh.write(text)
        fh.close()
        os.chmod(fh.name, mode)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    # -- parsing ---------------------------------------------------------

    def test_address_comment_is_not_a_recipient(self):
        aliases = m.parse_aliases("alerts: oncall@x.example (primary)\n")
        self.assertEqual(aliases["alerts"], ["oncall@x.example"])

    def test_quoted_local_part_survives(self):
        aliases = m.parse_aliases('odd: "a,b"@x.example\n')
        self.assertEqual(aliases["odd"], ['"a,b"@x.example'])

    def test_continuation_without_trailing_comma(self):
        aliases = m.parse_aliases("ops: a@x.example\n  b@x.example\n")
        self.assertEqual(aliases["ops"], ["a@x.example", "b@x.example"])

    def test_malformed_line_content_not_echoed(self):
        stderr = io.StringIO()
        with mock.patch.object(m, "_VERBOSE", True), \
             mock.patch.object(m.sys, "stderr", stderr):
            m.parse_aliases("client_secret = SENTINEL-SECRET\n")
        self.assertNotIn("SENTINEL-SECRET", stderr.getvalue())

    # -- expansion -------------------------------------------------------

    def test_overlapping_aliases_expand_each_name_once(self):
        # a0 -> a1 a1, a1 -> a2 a2, ... is 2**60 paths without memoizing.
        aliases = {f"a{i}": [f"a{i + 1}", f"a{i + 1}"] for i in range(60)}
        aliases["a60"] = ["ops@contoso.com"]
        r = _resolver(_cfg(aliases=aliases))
        self.assertEqual(r.resolve("a0"), ["ops@contoso.com"])

    def test_wide_alias_dedupes_quickly(self):
        wide = [f"u{i}@contoso.com" for i in range(20000)] * 2
        r = _resolver(_cfg(aliases={"all": wide, "a": ["all"], "b": ["all"]}))
        import time as _t
        start = _t.monotonic()
        result = r.resolve_recipients([m._recipient("a"), m._recipient("b")])
        self.assertLess(_t.monotonic() - start, 2.0)
        self.assertEqual(len(result), 20000)

    def test_overlapping_aliases_within_depth(self):
        aliases = {f"a{i}": [f"a{i + 1}", f"a{i + 1}"] for i in range(10)}
        aliases["a10"] = ["ops@contoso.com"]
        r = _resolver(_cfg(aliases=aliases))
        self.assertEqual(r.resolve("a0"), ["ops@contoso.com"])

    def test_too_deep_chain_is_config_error_not_catch_all(self):
        n = m.AddressResolver.MAX_DEPTH + 5
        aliases = {f"a{i}": [f"a{i + 1}"] for i in range(n)}
        aliases[f"a{n}"] = ["ops@contoso.com"]
        r = _resolver(_cfg(aliases=aliases, local_recipient="c@contoso.com"))
        with self.assertRaises(m.UserError) as ctx:
            r.resolve("a0")
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_dev_null_discards(self):
        r = _resolver(_cfg(aliases={"discard": ["/dev/null"]},
                           local_recipient="c@contoso.com"))
        self.assertEqual(r.resolve("discard"), [])

    def test_only_unsupported_targets_not_sent_to_catch_all(self):
        r = _resolver(_cfg(aliases={"bot": ["|/bin/cat"]},
                           local_recipient="c@contoso.com"))
        self.assertIsNone(r.resolve("bot"))

    def test_all_recipients_discarded_exits_ok_without_sending(self):
        cfg = _cfg(default_from="noreply@contoso.com",
                   aliases={"nobody": ["/dev/null"]})
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(b"Subject: s\n\nb\n")
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m, "load_config", return_value=cfg), \
             mock.patch.object(m, "get_token") as get_token:
            code = m.run(["nobody"])
        self.assertEqual(code, m.EX_OK)
        get_token.assert_not_called()

    # -- aliases file safety -----------------------------------------------

    def test_unreadable_aliases_file_is_an_error_not_catch_all(self):
        r = _resolver(_cfg(aliases_file="/", local_recipient="c@contoso.com"))
        with self.assertRaises(m.UserError) as ctx:
            r.resolve("root")
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_world_writable_aliases_file_refused(self):
        path = self._aliases_file("root: evil@x.example\n", 0o666)
        r = _resolver(_cfg(aliases_file=path))
        with self.assertRaises(m.UserError) as ctx:
            r.resolve("root")
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_oversized_aliases_file_refused(self):
        path = self._aliases_file("# x\n" * (m.MAX_ALIASES_BYTES // 4 + 1))
        r = _resolver(_cfg(aliases_file=path))
        with self.assertRaises(m.UserError):
            r.resolve("root")

    def test_fifo_aliases_file_refused_without_blocking(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "aliases")
        os.mkfifo(path)
        self.addCleanup(os.rmdir, d)
        self.addCleanup(os.unlink, path)
        r = _resolver(_cfg(aliases_file=path))
        with self.assertRaises(m.UserError):
            r.resolve("root")

    # -- domains -----------------------------------------------------------

    def test_from_domain_with_trailing_dot_still_protected(self):
        cfg = _cfg(default_from="relay@mail.example.")
        r = _resolver(cfg, hostname="mail", fqdn="mail.example")
        self.assertFalse(r.is_local("alice@mail.example"))

    def test_explicit_local_domain_with_trailing_dot(self):
        r = _resolver(_cfg(local_domains={"corp.lan."}))
        self.assertTrue(r.is_local("root@corp.lan"))

    def test_local_catch_all_is_config_error(self):
        r = _resolver(_cfg(local_recipient="root@localhost"))
        with self.assertRaises(m.UserError) as ctx:
            r.resolve("deploy")
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    # -- sender ------------------------------------------------------------

    def test_local_dash_f_uses_configured_from_not_header(self):
        cfg = _cfg(default_from="noreply@contoso.com")
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(
            b"From: finance@contoso.com\nSubject: s\n\nb\n")
        stdout = io.StringIO()
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m.sys, "stdout", stdout), \
             mock.patch.object(m, "load_config", return_value=cfg):
            m.run(["-f", "www-data", "alice@contoso.com", "--dry-run"])
        self.assertEqual(json.loads(stdout.getvalue())["mailbox"],
                         "noreply@contoso.com")

    # -- options / config --------------------------------------------------

    def test_separate_b_v_is_verify(self):
        opts = m.parse_args(["-b", "v", "root"])
        self.assertTrue(opts.verify)
        self.assertEqual(opts.recipients, ["root"])

    def test_other_b_modes_do_not_become_recipients(self):
        opts = m.parse_args(["-b", "m", "alice@x.example"])
        self.assertFalse(opts.verify)
        self.assertEqual(opts.recipients, ["alice@x.example"])

    def _conf(self, text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write(text)
        os.chmod(fh.name, 0o600)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_escaped_percent_keeps_working(self):
        cfg = m.load_config(self._conf(
            "[msgraph]\nclient_secret = ab%%cd\n"
            "local_recipient = ops%%a@contoso.com\n"))
        self.assertEqual(cfg.client_secret, "ab%cd")
        self.assertEqual(cfg.local_recipient, "ops%a@contoso.com")

    def test_bad_percent_is_config_error(self):
        with self.assertRaises(m.UserError) as ctx:
            m.load_config(self._conf("[msgraph]\nlocal_recipient = a%b@c.d\n"))
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_configured_addresses_are_parsed(self):
        cfg = m.load_config(self._conf(
            "[msgraph]\nfrom = Alerts <relay@contoso.com>\n"
            "local_recipient = Ops <ops@contoso.com>\n"))
        self.assertEqual(cfg.default_from, "relay@contoso.com")
        self.assertEqual(cfg.local_recipient, "ops@contoso.com")

    def test_local_recipient_with_display_name_still_checked_for_local(self):
        cfg = m.load_config(self._conf(
            "[msgraph]\nlocal_recipient = Ops <root@localhost>\n"))
        r = _resolver(_cfg(local_recipient=cfg.local_recipient))
        with self.assertRaises(m.UserError):
            r.resolve("deploy")

    def test_multiple_local_recipients_rejected(self):
        with self.assertRaises(m.UserError):
            m.load_config(self._conf(
                "[msgraph]\nlocal_recipient = a@contoso.com, b@contoso.com\n"))

    def test_escaped_paren_in_comment(self):
        aliases = m.parse_aliases(
            "ops: owner@x.example (old \\), former@x.example, )\n")
        self.assertEqual(aliases["ops"], ["owner@x.example"])

    def test_display_name_split_across_continuation(self):
        aliases = m.parse_aliases("ops: Operations\n  <ops@x.example>\n")
        self.assertEqual(aliases["ops"], ["ops@x.example"])

    def test_bare_b_followed_by_option_is_usage_error(self):
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["-b", "--dry-run", "alice@x.example"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def test_bare_b_at_end_is_usage_error(self):
        with self.assertRaises(m.UserError):
            m.parse_args(["-b"])

    def test_unicode_domain_not_folded_into_ascii_lookalike(self):
        r = _resolver(_cfg(local_domains={"fass.example"}))
        self.assertFalse(r.is_local("alice@fa\u00df.example"))

    def test_discarded_reply_to_does_not_mask_missing_recipients(self):
        cfg = _cfg(default_from="noreply@contoso.com",
                   aliases={"discard": ["/dev/null"]})
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(
            b"Reply-To: discard\nSubject: s\n\nb\n")
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m, "load_config", return_value=cfg):
            with self.assertRaises(m.UserError) as ctx:
                m.run(["-t", "--dry-run"])
        self.assertEqual(ctx.exception.exit_code, m.EX_DATAERR)



class TestReviewRound3(unittest.TestCase):
    """Regression tests for the third Codex review round of #1."""

    def _conf(self, text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write(text)
        os.chmod(fh.name, 0o600)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def _config_error(self, text):
        with self.assertRaises(m.UserError) as ctx:
            m.load_config(self._conf(text))
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)
        return str(ctx.exception)

    # -- secrets never reach error messages ------------------------------

    def test_interpolation_error_does_not_leak_secret(self):
        msg = self._config_error(
            "[msgraph]\nclient_secret = abc%SENTINEL\n")
        self.assertNotIn("SENTINEL", msg)
        self.assertIn("client_secret", msg)

    def test_missing_section_header_does_not_leak_line(self):
        msg = self._config_error("client_secret = SENTINEL\n")
        self.assertNotIn("SENTINEL", msg)

    def test_syntax_error_does_not_leak_line(self):
        msg = self._config_error("[msgraph]\ntenant = t\nSENTINEL-LINE\n")
        self.assertNotIn("SENTINEL", msg)
        self.assertIn("3", msg)

    def test_default_section_does_not_leak_into_aliases(self):
        cfg = m.load_config(self._conf(
            "[DEFAULT]\nclient_secret = demo%%tag@x.example\n"
            "[msgraph]\ntenant = t\n"
            "[aliases]\nroot = ops@contoso.com\n"))
        self.assertEqual(cfg.aliases, {"root": ["ops@contoso.com"]})

    def test_explicit_alias_equal_to_default_is_kept(self):
        cfg = m.load_config(self._conf(
            "[DEFAULT]\nroot = ops@contoso.com\n"
            "[msgraph]\ntenant = t\n"
            "[aliases]\nroot = ops@contoso.com\n"))
        self.assertEqual(cfg.aliases, {"root": ["ops@contoso.com"]})

    # -- expansion -------------------------------------------------------

    def test_cycle_result_independent_of_lookup_order(self):
        aliases = {"a": ["b", "audit@contoso.com"], "b": ["a"]}
        first = _resolver(_cfg(aliases=aliases, local_recipient="c@contoso.com"))
        first.resolve("a")
        via_a_first = first.resolve("b")
        fresh = _resolver(_cfg(aliases=aliases, local_recipient="c@contoso.com"))
        self.assertEqual(sorted(via_a_first), sorted(fresh.resolve("b")))
        self.assertIn("audit@contoso.com", via_a_first)

    def test_many_aliases_sharing_a_big_list(self):
        big = [f"u{i}@contoso.com" for i in range(5000)]
        aliases = {f"l{i}": ["big"] for i in range(200)}
        aliases["big"] = big
        r = _resolver(_cfg(aliases=aliases))
        import time as _t
        start = _t.monotonic()
        result = r.resolve_recipients(
            [m._recipient(f"l{i}") for i in range(200)])
        self.assertLess(_t.monotonic() - start, 5.0)
        self.assertEqual(len(result), 5000)

    def test_expansion_budget_is_enforced(self):
        aliases = {f"l{i}": ["big"] for i in range(50)}
        aliases["big"] = [f"u{i}@contoso.com" for i in range(100)]
        r = _resolver(_cfg(aliases=aliases))
        with mock.patch.object(m.AddressResolver, "MAX_EXPANSION_STEPS", 50):
            with self.assertRaises(m.UserError) as ctx:
                r.resolve_recipients(
                    [m._recipient(f"l{i}") for i in range(50)])
        self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_shared_child_is_complete_for_every_parent(self):
        aliases = {"s": ["a", "b"], "a": ["big"], "b": ["big"],
                   "big": ["x@contoso.com", "y@contoso.com"]}
        r = _resolver(_cfg(aliases=aliases))
        self.assertEqual(r.resolve("s"), ["x@contoso.com", "y@contoso.com"])
        self.assertEqual(r.resolve("b"), ["x@contoso.com", "y@contoso.com"])

    def test_shared_cyclic_child_is_complete_for_every_parent(self):
        aliases = {"s": ["a", "b"], "a": ["c"], "b": ["c"],
                   "c": ["s", "x@contoso.com"]}
        cfg = _cfg(aliases=aliases, local_recipient="f@contoso.com")
        r = _resolver(cfg)
        r.resolve("s")
        for name in ("a", "b", "c"):
            fresh = _resolver(_cfg(aliases=aliases,
                                   local_recipient="f@contoso.com"))
            self.assertEqual(sorted(r.resolve(name)),
                             sorted(fresh.resolve(name)), name)

    def test_folded_mailbox_is_not_split(self):
        aliases = m.parse_aliases("alerts: ops@\n  x.example\n")
        self.assertEqual(aliases["alerts"], ["ops@x.example"])

    # -- configured mailboxes --------------------------------------------

    def test_catch_all_without_local_part_rejected(self):
        self._config_error("[msgraph]\nlocal_recipient = @x.example\n")

    def test_catch_all_with_trailing_empty_address_rejected(self):
        self._config_error(
            "[msgraph]\nlocal_recipient = Ops <ops@x.example>, <>\n")

    def test_env_catch_all_is_validated(self):
        with mock.patch.dict(os.environ, {
                "MSGRAPH_SENDMAIL_LOCAL_RECIPIENT": "@x.example"}):
            self._config_error("[msgraph]\n")

    def test_from_without_domain_rejected(self):
        self._config_error("[msgraph]\nfrom = noreply\n")

    # -- -b modes --------------------------------------------------------

    def test_bare_b_followed_by_address_is_usage_error(self):
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["-b", "alice@x.example", "bob@x.example"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def test_smtp_mode_is_rejected_clearly(self):
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["-bs"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def _run_noop(self, argv):
        stdin = mock.Mock()
        stdin.buffer = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(m.sys, "stdin", stdin), \
             mock.patch.object(m.sys, "stdout", stdout), \
             mock.patch.object(m, "load_config", return_value=_cfg()):
            code = m.run(argv)
        stdin.buffer.read.assert_not_called()
        return code, stdout.getvalue()

    def test_newaliases_mode_is_a_noop(self):
        self.assertEqual(self._run_noop(["-bi"])[0], m.EX_OK)

    def test_queue_listing_reports_empty_queue(self):
        code, out = self._run_noop(["-bp"])
        self.assertEqual(code, m.EX_OK)
        self.assertIn("queue is empty", out)


if __name__ == "__main__":
    unittest.main()
