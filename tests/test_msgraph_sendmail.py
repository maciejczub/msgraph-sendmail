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


if __name__ == "__main__":
    unittest.main()
