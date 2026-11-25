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

    def close(self):
        pass

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
        with mock.patch.object(m, "_open",
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
        with mock.patch.object(m, "_open",
                               side_effect=responses), \
             mock.patch.object(m.time, "sleep") as sleep:
            m.send_message(self._cfg(), "tok", "s@x.example",
                           {"subject": "t"}, True)
        self.assertTrue(sleep.called)

    def test_permanent_400_raises_dataerr(self):
        response = _FakeResponse(
            400, b'{"error":{"code":"ErrorInvalidRecipients",'
                 b'"message":"bad recipient"}}')
        with mock.patch.object(m, "_open",
                               return_value=response):
            with self.assertRaises(m.UserError) as ctx:
                m.send_message(self._cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True)
        self.assertEqual(ctx.exception.exit_code, m.EX_DATAERR)

    def test_403_raises_unavailable(self):
        response = _FakeResponse(
            403, b'{"error":{"code":"ErrorAccessDenied",'
                 b'"message":"denied"}}')
        with mock.patch.object(m, "_open",
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
        with mock.patch.object(m, "_open",
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



# ---------------------------------------------------------------------------
# Delivery time budget (issue #2)
# ---------------------------------------------------------------------------

class FakeClock:
    """Monotonic clock + sleep that only advance when told to."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _net_cfg(**kwargs):
    cfg = m.Config()
    cfg.tenant, cfg.client_id, cfg.client_secret = "t", "c", "s"
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


class FakeHttp:
    """Stand-in for _http_request: each step is (duration, result) where
    result is a (status, body, headers) tuple or an exception to raise."""

    def __init__(self, clock, steps):
        self.clock = clock
        self.steps = list(steps)
        self.timeouts = []

    def __call__(self, url, data, headers, timeout, on_accepted=None):
        self.timeouts.append(timeout)
        duration, result = self.steps.pop(0)
        self.clock.now += min(duration, timeout)
        if duration > timeout:
            raise m.NetworkError("timed out", maybe_sent=True)
        if isinstance(result, BaseException):
            raise result
        if result[0] == 202 and on_accepted is not None:
            on_accepted()
        return result


ACCEPTED = (202, b"", {})
TOKEN_OK = (200, b'{"access_token": "tok"}', {})


def _busy(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return (503, b'{"error":{"code":"ServiceUnavailable","message":"busy"}}',
            headers)


class TestDeadline(unittest.TestCase):
    def test_remaining_and_elapsed(self):
        clock = FakeClock()
        d = m.Deadline(50, clock=clock)
        clock.now += 20
        self.assertAlmostEqual(d.remaining(), 30)
        self.assertAlmostEqual(d.elapsed(), 20)

    def test_zero_means_unlimited(self):
        d = m.Deadline(0, clock=FakeClock())
        self.assertEqual(d.remaining(), float("inf"))


class TestRetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(m._parse_retry_after("7"), 7.0)

    def test_http_date(self):
        now = 1_700_000_000.0
        value = email.utils.formatdate(now + 12, usegmt=True)
        self.assertAlmostEqual(m._parse_retry_after(value, now=now), 12,
                               delta=1)

    def test_garbage_and_negative(self):
        self.assertIsNone(m._parse_retry_after("soon"))
        self.assertEqual(m._parse_retry_after("-5"), 0.0)


class TestSendBudget(unittest.TestCase):
    def _send(self, steps, cfg=None, max_time=50):
        clock = FakeClock()
        http = FakeHttp(clock, steps)
        deadline = m.Deadline(max_time, clock=clock)
        with mock.patch.object(m, "_http_request", http), \
             mock.patch.object(m.time, "sleep", clock.sleep), \
             mock.patch.object(m, "_syslog_write") as syslog:
            try:
                m.send_message(cfg or _net_cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True, deadline=deadline)
                error = None
            except m.UserError as exc:
                error = exc
        return error, clock, http, syslog

    def test_5xx_retried_then_accepted(self):
        error, clock, http, syslog = self._send([(1, _busy()), (1, ACCEPTED)])
        self.assertIsNone(error)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertTrue(syslog.called)  # the retry is logged

    def test_retry_after_honoured_when_short(self):
        error, clock, _, _ = self._send([(1, _busy("10")), (1, ACCEPTED)])
        self.assertIsNone(error)
        self.assertEqual(clock.sleeps, [10.0])

    def test_long_retry_after_gives_up_immediately(self):
        error, clock, http, _ = self._send([(1, _busy("120"))])
        self.assertEqual(error.exit_code, m.EX_TEMPFAIL)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(http.steps, [])
        self.assertIn("Retry-After", str(error))

    def test_retry_after_beyond_deadline_gives_up(self):
        error, clock, _, _ = self._send([(1, _busy("20"))], max_time=15)
        self.assertEqual(error.exit_code, m.EX_TEMPFAIL)
        self.assertEqual(clock.sleeps, [])

    def test_slow_server_stays_within_max_time(self):
        steps = [(60, None)] * 10
        error, clock, http, _ = self._send(steps, _net_cfg(timeout=15))
        self.assertEqual(error.exit_code, m.EX_TEMPFAIL)
        self.assertLessEqual(clock.now - 1000.0, 50.0 + 1e-6)
        self.assertTrue(all(t <= 15 for t in http.timeouts))
        self.assertIn("max_time", str(error))

    def test_socket_timeout_capped_by_remaining_budget(self):
        error, clock, http, _ = self._send(
            [(1, _busy()), (1, ACCEPTED)], _net_cfg(timeout=15), max_time=5)
        self.assertIsNone(error)
        self.assertEqual(http.timeouts[0], 5)
        self.assertLessEqual(http.timeouts[1], 2.0)

    def test_network_error_before_send_is_retried(self):
        error, clock, http, syslog = self._send([
            (0, m.NetworkError("connection refused", maybe_sent=False)),
            (1, ACCEPTED)])
        self.assertIsNone(error)
        self.assertEqual(clock.sleeps, [2.0])

    def test_network_error_after_send_retried_with_duplicate_warning(self):
        error, clock, http, syslog = self._send([
            (0, m.NetworkError("connection reset", maybe_sent=True)),
            (1, ACCEPTED)])
        self.assertIsNone(error)
        logged = " ".join(str(c) for c in syslog.call_args_list)
        self.assertIn("twice", logged)

    def test_non_retryable_network_error_fails_at_once(self):
        error, clock, http, _ = self._send([
            (0, m.NetworkError("certificate verify failed", maybe_sent=False,
                               retryable=False)),
            (1, ACCEPTED)])
        self.assertEqual(error.exit_code, m.EX_TEMPFAIL)
        self.assertEqual(len(http.steps), 1)

    def test_network_errors_exhaust_attempts(self):
        steps = [(0, m.NetworkError("refused", maybe_sent=False))] * 4
        error, clock, http, _ = self._send(steps, max_time=0)
        self.assertEqual(error.exit_code, m.EX_TEMPFAIL)
        self.assertEqual(http.steps, [])
        self.assertIn("refused", str(error))

    def test_permanent_error_not_retried(self):
        error, clock, http, _ = self._send([
            (0, (400, b'{"error":{"code":"ErrorInvalidRecipients",'
                      b'"message":"bad"}}', {})), (0, ACCEPTED)])
        self.assertEqual(error.exit_code, m.EX_DATAERR)
        self.assertEqual(len(http.steps), 1)


class TestTokenBudget(unittest.TestCase):
    def _token(self, steps, max_time=50):
        clock = FakeClock()
        http = FakeHttp(clock, steps)
        with mock.patch.object(m, "_http_request", http), \
             mock.patch.object(m.time, "sleep", clock.sleep), \
             mock.patch.object(m, "_syslog_write"):
            try:
                return m.get_token(_net_cfg(),
                                   deadline=m.Deadline(max_time, clock=clock)), \
                    None, http
            except m.UserError as exc:
                return None, exc, http

    def test_token_5xx_retried(self):
        token, error, _ = self._token([(1, _busy()), (1, TOKEN_OK)])
        self.assertEqual(token, "tok")

    def test_token_network_error_retried(self):
        token, error, _ = self._token([
            (0, m.NetworkError("dns failure", maybe_sent=False)),
            (1, TOKEN_OK)])
        self.assertEqual(token, "tok")

    def test_token_auth_error_not_retried(self):
        token, error, http = self._token([
            (0, (401, b'{"error":"invalid_client",'
                      b'"error_description":"AADSTS7000215"}', {})),
            (0, TOKEN_OK)])
        self.assertEqual(error.exit_code, m.EX_UNAVAILABLE)
        self.assertEqual(len(http.steps), 1)


class TestHttpErrorClassification(unittest.TestCase):
    def _classify(self, exc):
        with mock.patch.object(m, "_open", side_effect=exc):
            with self.assertRaises(m.NetworkError) as ctx:
                m._http_request("https://x.example/", b"", {}, 5)
        return ctx.exception

    def test_connect_failure_was_not_sent(self):
        err = self._classify(m.urllib.error.URLError(ConnectionRefusedError()))
        self.assertFalse(err.maybe_sent)
        self.assertTrue(err.retryable)

    def test_response_timeout_may_have_been_sent(self):
        err = self._classify(TimeoutError("timed out"))
        self.assertTrue(err.maybe_sent)
        self.assertTrue(err.retryable)

    def test_incomplete_read_may_have_been_sent(self):
        import http.client
        err = self._classify(http.client.IncompleteRead(b""))
        self.assertTrue(err.maybe_sent)

    def test_certificate_failure_not_retryable(self):
        import ssl
        err = self._classify(m.urllib.error.URLError(
            ssl.SSLCertVerificationError("bad cert")))
        self.assertFalse(err.retryable)


class TestBudgetConfig(unittest.TestCase):
    def _conf(self, text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write(text)
        os.chmod(fh.name, 0o600)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_defaults(self):
        cfg = m.Config()
        self.assertEqual(cfg.timeout, 15.0)
        self.assertEqual(cfg.max_time, 50.0)

    def test_file_and_env(self):
        path = self._conf("[msgraph]\nmax_time = 40\n")
        self.assertEqual(m.load_config(path).max_time, 40.0)
        with mock.patch.dict(os.environ, {"MSGRAPH_SENDMAIL_MAX_TIME": "25"}):
            self.assertEqual(m.load_config(path).max_time, 25.0)

    def test_invalid_max_time(self):
        for bad in ("-1", "soon"):
            with self.assertRaises(m.UserError) as ctx:
                m.load_config(self._conf(f"[msgraph]\nmax_time = {bad}\n"))
            self.assertEqual(ctx.exception.exit_code, m.EX_CONFIG)

    def test_cli_option(self):
        self.assertEqual(m.parse_args(["--max-time", "30", "a@x.example"])
                         .max_time, 30.0)
        self.assertEqual(m.parse_args(["--max-time=0", "a@x.example"])
                         .max_time, 0.0)
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["--max-time", "x", "a@x.example"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def test_cli_option_does_not_switch_to_cli_mode(self):
        self.assertEqual(m.parse_args(["--max-time", "30", "-t"]).mode,
                         "sendmail")


def _reset_abort_state():
    m._ABORTING = m._FINISHING = m._REPORTING = False
    m._PENDING_ABORT = None
    m._PROGRESS.clear()
    m._PROGRESS.update(stage="startup", attempt=0)


class TestAbortLogging(unittest.TestCase):
    def setUp(self):
        _reset_abort_state()
        self.addCleanup(_reset_abort_state)

    def test_sigterm_is_logged_and_exits_tempfail(self):
        import signal as _signal

        def fake_run(argv):
            m._PROGRESS.update(stage="send", attempt=2)
            m._on_signal(_signal.SIGTERM, None)

        with mock.patch.object(m, "run", fake_run), \
             mock.patch.object(m, "_syslog_write") as syslog, \
             mock.patch.object(m.sys, "stderr", io.StringIO()), \
             mock.patch.object(m, "_install_signal_handlers"):
            with self.assertRaises(SystemExit) as ctx:
                m.main()
        self.assertEqual(ctx.exception.code, m.EX_TEMPFAIL)
        logged = " ".join(str(c) for c in syslog.call_args_list)
        self.assertIn("SIGTERM", logged)
        self.assertIn("stage: send", logged)
        self.assertIn("attempt 2", logged)

    def test_give_up_is_logged_to_syslog(self):
        def fake_run(argv):
            raise m.UserError("gave up", m.EX_TEMPFAIL)

        with mock.patch.object(m, "run", fake_run), \
             mock.patch.object(m, "_syslog_write") as syslog, \
             mock.patch.object(m.sys, "stderr", io.StringIO()), \
             mock.patch.object(m, "_install_signal_handlers"):
            with self.assertRaises(SystemExit) as ctx:
                m.main()
        self.assertEqual(ctx.exception.code, m.EX_TEMPFAIL)
        self.assertTrue(syslog.called)


class SlowServer:
    """A local HTTP server that is slow on purpose.

    mode "silent": accept the request, then say nothing for *delay* s.
    mode "trickle": send headers, then one body byte every 0.3 s for
    *delay* s — every socket read succeeds, so only an overall deadline
    (not a socket timeout) can stop the client.
    """

    def __init__(self, delay, mode="silent"):
        import http.server
        import threading
        self.hit = threading.Event()
        self.stop = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hit.set()
                try:
                    if mode == "trickle":
                        self.send_response(200)
                        self.send_header("Content-Length", "100000")
                        self.end_headers()
                        self.wfile.flush()
                        end = time.monotonic() + delay
                        while time.monotonic() < end \
                                and not outer.stop.wait(0.3):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                    else:
                        outer.stop.wait(delay)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


import time  # noqa: E402  (used by SlowServer)


class TestBudgetIntegration(unittest.TestCase):
    """Real processes against a real (slow) local HTTP server."""

    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def _spawn(self, server, extra_args, cli=True, stdin=None,
               block_sigalrm=False):
        import subprocess
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); "
                "os.environ.get('TEST_BLOCK_SIGALRM') and "
                "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM}); "
                "import msgraph_sendmail as m; "
                "m.LOGIN_BASE = sys.argv[2]; m.GRAPH_BASE = sys.argv[2]; "
                "sys.argv = ['msgraph-sendmail'] + sys.argv[3:]; m.main()")
        # Allowlisted environment: never pass the developer's real
        # MSGRAPH_SENDMAIL_* settings (e.g. CLIENT_SECRET_FILE) or proxy
        # variables to a child that talks plain HTTP.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
               if key in os.environ}
        env.update(MSGRAPH_SENDMAIL_CONFIG=os.devnull,
                   MSGRAPH_SENDMAIL_TENANT="t",
                   MSGRAPH_SENDMAIL_CLIENT_ID="c",
                   MSGRAPH_SENDMAIL_CLIENT_SECRET="dummy-secret",
                   MSGRAPH_SENDMAIL_FROM="s@x.example",
                   NO_PROXY="*", no_proxy="*")
        if block_sigalrm:
            env["TEST_BLOCK_SIGALRM"] = "1"
        args = (["--to", "a@x.example", "--body", "x"] if cli
                else ["a@x.example"])
        proc = subprocess.Popen(
            [sys.executable, "-c", code, self.ROOT, server.url]
            + args + extra_args,
            env=env, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        self.addCleanup(reap)
        return proc

    def test_hard_deadline_against_silent_server(self):
        server = SlowServer(delay=30)
        self.addCleanup(server.close)
        start = time.monotonic()
        proc = self._spawn(server, ["--max-time", "2"])
        _, stderr = proc.communicate(timeout=20)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL, stderr)
        self.assertLess(elapsed, 6)
        self.assertIn("max_time", stderr)

    def test_hard_deadline_against_trickling_server(self):
        # Each read succeeds within the socket timeout; only the real-time
        # timer can stop this.
        server = SlowServer(delay=30, mode="trickle")
        self.addCleanup(server.close)
        start = time.monotonic()
        proc = self._spawn(server, ["--max-time", "2"])
        _, stderr = proc.communicate(timeout=20)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL, stderr)
        self.assertLess(elapsed, 6)
        self.assertIn("max_time of 2s exceeded", stderr)
        self.assertIn("stage: token", stderr)

    def test_closed_stderr_keeps_exit_status(self):
        server = SlowServer(delay=30)
        self.addCleanup(server.close)
        proc = self._spawn(server, ["--max-time", "1"])
        proc.stderr.close()               # caller stopped reading stderr
        proc.wait(timeout=15)
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL)

    def test_inherited_blocked_sigalrm_does_not_disable_deadline(self):
        import subprocess
        server = SlowServer(delay=30)
        self.addCleanup(server.close)
        start = time.monotonic()
        proc = self._spawn(server, ["--max-time", "2"], cli=False,
                           stdin=subprocess.PIPE, block_sigalrm=True)
        try:
            proc.wait(timeout=10)
        finally:
            proc.stdin.close()
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL)
        self.assertLess(time.monotonic() - start, 6)

    def test_deadline_covers_reading_stdin(self):
        import subprocess
        server = SlowServer(delay=30)
        self.addCleanup(server.close)
        start = time.monotonic()
        proc = self._spawn(server, ["--max-time", "2"], cli=False,
                           stdin=subprocess.PIPE)  # never written/closed
        try:
            proc.wait(timeout=10)
        finally:
            proc.stdin.close()
        stderr = proc.stderr.read()
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL, stderr)
        self.assertLess(time.monotonic() - start, 6)
        self.assertIn("stage: input", stderr)
        self.assertFalse(server.hit.is_set())

    def test_sigterm_during_send_is_reported(self):
        import signal as _signal
        server = SlowServer(delay=30)
        self.addCleanup(server.close)
        proc = self._spawn(server, ["--max-time", "20"])
        self.assertTrue(server.hit.wait(10), "request never arrived")
        proc.send_signal(_signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, m.EX_TEMPFAIL, stderr)
        self.assertIn("aborted by SIGTERM", stderr)
        self.assertIn("stage: token", stderr)



class TestBudgetReviewFindings(unittest.TestCase):
    """Regression tests for the Codex review of #2."""

    def setUp(self):
        _reset_abort_state()
        self.addCleanup(_reset_abort_state)

    def test_accepted_202_with_broken_body_is_not_retried(self):
        import http.client

        class Broken(_FakeResponse):
            def read(self):
                raise http.client.IncompleteRead(b"")

        with mock.patch.object(m, "_open", return_value=Broken(202, b"")):
            status, body, _ = m._http_request("https://x.example/", b"",
                                              {}, 5)
        self.assertEqual(status, 202)

    def test_oauth_style_error_body_is_retried_not_crashing(self):
        clock = FakeClock()
        http_ = FakeHttp(clock, [
            (1, (503, b'{"error": "temporarily_unavailable"}', {})),
            (1, TOKEN_OK)])
        with mock.patch.object(m, "_http_request", http_), \
             mock.patch.object(m.time, "sleep", clock.sleep), \
             mock.patch.object(m, "_syslog_write"):
            token = m.get_token(_net_cfg(),
                                deadline=m.Deadline(50, clock=clock))
        self.assertEqual(token, "tok")

    def test_error_details_tolerate_odd_bodies(self):
        self.assertEqual(m._graph_error_details(b'["x"]'), ("", ""))
        self.assertEqual(
            m._graph_error_details(b'{"error": "invalid_client", '
                                   b'"error_description": "bad"}'),
            ("invalid_client", "bad"))

    def _http_error(self, code, headers, body_reads):
        import http.client
        msg = http.client.HTTPMessage()
        for key, value in headers.items():
            msg[key] = value

        class Body(io.BytesIO):
            def read(self_inner, *args):
                body_reads.append(1)
                return b'{"error": "x", "error_description": "AADSTS42"}'
        return m.urllib.error.HTTPError("https://x.example/", code, "err",
                                        msg, Body())

    def test_lowercase_retry_after_header_is_honoured(self):
        reads = []
        exc = self._http_error(503, {"retry-after": "120"}, reads)
        with mock.patch.object(m, "_open", side_effect=exc), \
             mock.patch.object(m.time, "sleep") as sleep, \
             mock.patch.object(m, "_syslog_write"):
            with self.assertRaises(m.UserError) as ctx:
                m.send_message(_net_cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True)
        self.assertEqual(ctx.exception.exit_code, m.EX_TEMPFAIL)
        sleep.assert_not_called()
        self.assertEqual(reads, [])          # stalled body never read
        self.assertTrue(exc.fp.closed)

    def test_retry_after_ignored_for_permanent_errors(self):
        reads = []
        exc = self._http_error(401, {"Retry-After": "120"}, reads)
        with mock.patch.object(m, "_open", side_effect=exc), \
             mock.patch.object(m, "_syslog_write"):
            with self.assertRaises(m.UserError) as ctx:
                m.get_token(_net_cfg())
        self.assertEqual(ctx.exception.exit_code, m.EX_UNAVAILABLE)
        self.assertIn("AADSTS42", str(ctx.exception))

    def test_overflowing_retry_after_date_is_ignored(self):
        for value in ("Mon, 01 Jan 2024 00:00:00 +99999999",
                      "Mon, 01 Jan 99999 00:00:00 GMT"):
            m._parse_retry_after(value)  # must not raise

    def test_accepted_marks_done_before_anything_else(self):
        clock = FakeClock()
        seen = []
        with mock.patch.object(m, "_http_request",
                               FakeHttp(clock, [(1, ACCEPTED)])), \
             mock.patch.object(m, "debug",
                               lambda msg: seen.append(m._PROGRESS["stage"])):
            m.send_message(_net_cfg(), "tok", "s@x.example", {"subject": "t"},
                           True, deadline=m.Deadline(50, clock=clock))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"done"})

    def test_swallowed_abort_is_re_raised_before_posting(self):
        clock = FakeClock()
        http_ = FakeHttp(clock, [(1, ACCEPTED)])
        m._PENDING_ABORT = "aborted by SIGTERM"
        self.addCleanup(setattr, m, "_PENDING_ABORT", None)
        with mock.patch.object(m, "_http_request", http_):
            with self.assertRaises(m.Aborted):
                m.send_message(_net_cfg(), "tok", "s@x.example",
                               {"subject": "t"}, True,
                               deadline=m.Deadline(50, clock=clock))
        self.assertEqual(len(http_.steps), 1)   # nothing was posted

    def test_bad_redirect_location_is_just_an_http_status(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://[::1")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            status, _, _ = m._http_request(
                f"http://127.0.0.1:{server.server_address[1]}/", b"x", {}, 5)
        thread.join(5)
        self.assertEqual(status, 302)

    def test_main_restores_signal_handlers(self):
        import signal as _signal
        before = _signal.getsignal(_signal.SIGTERM)
        with mock.patch.object(m, "run", return_value=0), \
             mock.patch.object(m.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                m.main()
        self.assertIs(_signal.getsignal(_signal.SIGTERM), before)

    def test_invalid_timing_value_not_echoed(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf",
                                         delete=False) as fh:
            fh.write("[msgraph]\ntimeout = SENTINEL-SECRET\n")
        os.chmod(fh.name, 0o600)
        self.addCleanup(os.unlink, fh.name)
        with self.assertRaises(m.UserError) as ctx:
            m.load_config(fh.name)
        self.assertNotIn("SENTINEL", str(ctx.exception))

    def test_absurd_max_time_rejected(self):
        with self.assertRaises(m.UserError) as ctx:
            m.parse_args(["--max-time", "1e300", "a@x.example"])
        self.assertEqual(ctx.exception.exit_code, m.EX_USAGE)

    def _main(self, fake_run, stderr=None):
        with mock.patch.object(m, "run", fake_run), \
             mock.patch.object(m, "_syslog_write") as syslog, \
             mock.patch.object(m.sys, "stderr", stderr or io.StringIO()), \
             mock.patch.object(m, "_install_signal_handlers"):
            with self.assertRaises(SystemExit) as ctx:
                m.main()
        return ctx.exception.code, syslog

    def test_signal_after_acceptance_exits_zero(self):
        import signal as _signal

        def fake_run(argv):
            m._PROGRESS.update(stage="done", attempt=1)
            m._on_signal(_signal.SIGTERM, None)

        code, syslog = self._main(fake_run)
        self.assertEqual(code, m.EX_OK)
        self.assertIn("accepted", " ".join(map(str, syslog.call_args_list)))

    def test_second_signal_is_ignored(self):
        import signal as _signal
        with self.assertRaises(m.Aborted):
            m._on_signal(_signal.SIGTERM, None)
        self.assertIsNone(m._on_signal(_signal.SIGTERM, None))

    def test_signal_while_reporting_an_error_keeps_its_exit_code(self):
        import signal as _signal

        def fake_run(argv):
            raise m.UserError("bad message", m.EX_DATAERR)

        real_log_error = m.log_error

        def log_error(message):
            m._on_signal(_signal.SIGTERM, None)
            real_log_error(message)

        with mock.patch.object(m, "log_error", log_error):
            code, _ = self._main(fake_run)
        self.assertEqual(code, m.EX_DATAERR)

    def test_broken_stderr_still_logs_to_syslog(self):
        class BrokenPipe(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()

        def fake_run(argv):
            raise m.UserError("gave up", m.EX_TEMPFAIL)

        code, syslog = self._main(fake_run, stderr=BrokenPipe())
        self.assertEqual(code, m.EX_TEMPFAIL)
        self.assertTrue(syslog.called)



class TestBudgetRound3(unittest.TestCase):
    """Regression tests for the final Codex review round of #2."""

    def setUp(self):
        _reset_abort_state()
        self.addCleanup(_reset_abort_state)

    def test_202_survives_a_failing_close(self):
        class CloseFails(_FakeResponse):
            def close(self):
                raise OSError("reset while closing")

            def __exit__(self, *exc):
                self.close()

        accepted = []
        with mock.patch.object(m, "_open",
                               return_value=CloseFails(202, b"")):
            status, _, _ = m._http_request(
                "https://x.example/", b"", {}, 5,
                on_accepted=lambda: accepted.append(1))
        self.assertEqual(status, 202)
        self.assertEqual(accepted, [1])

    def test_202_marks_done_before_close(self):
        stages = []

        class Watch(_FakeResponse):
            def close(self):
                stages.append(m._PROGRESS.get("stage"))

            def __exit__(self, *exc):
                self.close()

        with mock.patch.object(m, "_open", return_value=Watch(202, b"")):
            m.send_message(_net_cfg(), "tok", "s@x.example",
                           {"subject": "t"}, True)
        self.assertEqual(stages, ["done"])

    def test_swallowed_abort_stops_dry_run_before_output(self):
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(b"To: a@x.example\nSubject: s\n\nb\n")
        stdout = io.StringIO()
        m._PENDING_ABORT = "aborted by SIGTERM"
        with mock.patch.object(m.sys, "stdin", fake_stdin), \
             mock.patch.object(m.sys, "stdout", stdout), \
             mock.patch.object(m, "load_config",
                               return_value=_cfg(default_from="s@x.example")):
            with self.assertRaises(m.Aborted):
                m.run(["-t", "--dry-run"])
        self.assertEqual(stdout.getvalue(), "")

    def test_install_records_handlers_even_if_unmasking_raises(self):
        import signal as _signal
        before = _signal.getsignal(_signal.SIGTERM)
        previous = {}
        with mock.patch.object(m.signal, "pthread_sigmask",
                               side_effect=m.Aborted("aborted by SIGTERM")):
            with self.assertRaises(m.Aborted):
                m._install_signal_handlers(previous)
        self.assertIn(_signal.SIGTERM, previous)
        m._restore_signal_handlers(previous)
        self.assertIs(_signal.getsignal(_signal.SIGTERM), before)

    def test_restore_leaves_unknown_native_handlers_alone(self):
        import signal as _signal
        with mock.patch.object(m.signal, "signal") as set_handler:
            m._restore_signal_handlers({_signal.SIGTERM: None})
        set_handler.assert_not_called()

    def test_stderr_none_is_tolerated(self):
        with mock.patch.object(m.sys, "stderr", None):
            m._stderr("hello")  # must not raise


if __name__ == "__main__":
    unittest.main()
