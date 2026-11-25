# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-25

### Upgrading from 1.0
- Set `local_recipient` (or `[aliases]`) if anything on the host mails
  `root` or other local users — e.g. cron, mdadm, smartd. Without it such
  mail fails with exit 67 (it already failed in 1.0, at Graph).
- If you put a wrapper script in front of msgraph-sendmail to rewrite
  `root`, point `/usr/sbin/sendmail` straight at msgraph-sendmail and
  move the mapping into the config.
- The default `timeout` drops from 30 to 15 s and a new overall
  `max_time` of 50 s applies. Callers with a longer budget that relied on
  long waits can raise `max_time` (or set `0` for no limit).
- `-bs` and other unsupported `-b` modes now fail with exit 64 instead
  of being ignored.
- Config values containing a lone `%` were never valid; they are now
  reported as exit 78 instead of a traceback.

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
- Overall time budget (#2): `max_time` config key, `--max-time` option and
  `MSGRAPH_SENDMAIL_MAX_TIME` (default 50 s, `0` = no limit) cover the
  token request, all attempts and pauses; a real-time timer enforces it.
  Running out exits 75 with the stage and elapsed time in syslog.
- Network errors are retried like HTTP 429/5xx, and the token request is
  now retried as well. A retry after the request may already have reached
  Graph is logged with a possible-duplicate warning. Every retry is logged
  at warning level.
- SIGTERM, SIGHUP, SIGINT and SIGALRM are logged to syslog
  (`aborted by SIGTERM after 12.3s (stage: send, attempt 2; …)`) and exit
  75, or 0 when Graph had already accepted the message.
- Errors are written to syslog before stderr; a closed or full stderr
  can no longer change the exit status or suppress the syslog entry.

### Changed
- Default `timeout` lowered from 30 to 15 seconds (it applies to each
  network operation; see `max_time` for the whole run).
- `Retry-After` is honoured up to 30 s (seconds or HTTP date) and never
  beyond `max_time`; a longer request ends the run with exit 75 at once
  instead of sleeping.
- Truncated responses (`IncompleteRead`) and OAuth-style error bodies
  from Entra ID (`{"error": "temporarily_unavailable"}`) no longer escape
  as a traceback.
- HTTP redirects are no longer followed.
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

[1.1.0]: https://github.com/maciejczub/msgraph-sendmail/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/maciejczub/msgraph-sendmail/releases/tag/v1.0.0
