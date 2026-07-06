# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

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
