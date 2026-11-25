# msgraph-sendmail

A drop-in `sendmail` replacement for Linux that delivers mail through the
**Microsoft Graph API** instead of SMTP.

Microsoft is retiring basic-auth SMTP submission for Exchange Online and
recommends sending application mail through the Graph API. That breaks the
classic pattern of pointing your server's `sendmail` at an Office 365 SMTP
relay. `msgraph-sendmail` fills the gap: anything that can run
`/usr/sbin/sendmail` — cron, PHP `mail()`, logwatch, monitoring scripts,
legacy applications — keeps working unchanged, while delivery happens over
HTTPS via Graph.

**Highlights**

- Drop-in sendmail compatibility: reads RFC 822/MIME from stdin, supports
  `-t`, `-f`, `-i` and tolerates the rest of the traditional option zoo
- Friendly CLI mode too: `--to`, `--subject`, `--body`, `--attach`, `--html`, …
- Full MIME: plain text, HTML, attachments, inline images (`cid:`),
  To/Cc/Bcc, Reply-To, UTF-8 everywhere
- Single file, **Python 3.8+ standard library only** — no pip dependencies
- App-only authentication (OAuth 2.0 client credentials) — no user
  interaction, no password storage, works headless
- Local addresses handled like an MTA: mail to `root`, cron job owners or
  `www-data@thishost` is mapped via `[aliases]`, `/etc/aliases` and a
  catch-all `local_recipient`; check it with `-bv`
- Automatic retry with backoff on throttling (HTTP 429), server and
  network errors — all within an overall time budget (`max_time`), so
  callers with a timeout get a clean exit 75 and a syslog entry instead of
  being killed mid-send
- Meaningful `sysexits` exit codes and syslog logging (mail facility)

## Installation

`msgraph-sendmail` is a single Python file. Pick whichever method you like:

**Straight copy (no packaging tools needed):**

```sh
curl -fsSL -o msgraph_sendmail.py \
  https://github.com/maciejczub/msgraph-sendmail/releases/latest/download/msgraph_sendmail.py
install -m 755 msgraph_sendmail.py /usr/local/bin/msgraph-sendmail
```

(or `install` it straight from a clone of this repository)

**pipx / pip:**

```sh
pipx install git+https://github.com/maciejczub/msgraph-sendmail
# or: pip install git+https://github.com/maciejczub/msgraph-sendmail
```

To make it the system sendmail, see
[docs/sendmail-integration.md](docs/sendmail-integration.md).

## Setup

### 1. Register an app in Microsoft Entra ID

You need an app registration with the **`Mail.Send` application permission**
(Microsoft Graph → Application permissions) and **admin consent** granted.
Step-by-step instructions, including how to restrict the app to specific
mailboxes (strongly recommended): [docs/azure-setup.md](docs/azure-setup.md).

### 2. Create the config file

```sh
cp examples/msgraph-sendmail.conf /etc/msgraph-sendmail.conf
chmod 600 /etc/msgraph-sendmail.conf
$EDITOR /etc/msgraph-sendmail.conf
```

```ini
[msgraph]
tenant = contoso.com                # tenant GUID or a verified domain
client_id = <application-client-id>
client_secret = <client-secret-value>
from = noreply@contoso.com          # default sender mailbox
local_recipient = ops@contoso.com   # where mail for root & co. goes
```

Configuration is looked up in this order:
`--config FILE` → `$MSGRAPH_SENDMAIL_CONFIG` →
`~/.config/msgraph-sendmail.conf` → `/etc/msgraph-sendmail.conf`.
Every value can also be supplied via environment variables
(`MSGRAPH_SENDMAIL_TENANT`, `MSGRAPH_SENDMAIL_CLIENT_ID`,
`MSGRAPH_SENDMAIL_CLIENT_SECRET`, `MSGRAPH_SENDMAIL_CLIENT_SECRET_FILE`,
`MSGRAPH_SENDMAIL_FROM`, `MSGRAPH_SENDMAIL_LOCAL_RECIPIENT`,
`MSGRAPH_SENDMAIL_ALIASES_FILE`, `MSGRAPH_SENDMAIL_MAX_TIME`), which
override the file.
Full reference: [docs/configuration.md](docs/configuration.md).

### 3. Check where local mail goes, then send a test message

```sh
msgraph-sendmail -bv root www-data      # nothing is sent
msgraph-sendmail --to you@example.com --subject "Graph test" \
                 --body "Hello from msgraph-sendmail"
```

## Usage

### Sendmail mode (default)

Reads the message from stdin, like the classic binary:

```sh
# Recipients from the To:/Cc:/Bcc: headers (-t), as PHP and cron do
msgraph-sendmail -t <<'EOF'
To: alice@example.com
Subject: Disk report

All disks healthy.
EOF

# Recipients as arguments
echo "backup finished" | msgraph-sendmail -f robot@example.com admin@example.com
```

Supported sendmail options: `-t` (recipients from headers), `-f`/`-r`
(envelope sender), `-i`/`-oi` (don't treat a lone `.` as end of input),
`-v` (verbose), `-bv` (verify: show where each address would go, send
nothing), `-bi`/`-bp` (no-ops: no alias database, no queue). `-bs` (SMTP
on stdin) is not supported — configure such mailers to use `-t -i`. All
other classic options (`-F`, `-o…`, `-q…`, `-B`, …) are
accepted and ignored, so existing callers don't break.

### CLI mode

Triggered by using any of `--to/--cc/--bcc/--subject/--body/--body-file/
--html/--attach/--reply-to`:

```sh
msgraph-sendmail \
  --to "alice@example.com, bob@example.com" \
  --cc boss@example.com \
  --subject "Monthly report" \
  --body-file report.html --html \
  --attach report.pdf --attach data.xlsx \
  --reply-to reports@example.com
```

With no `--body`/`--body-file`, the body is read from stdin:

```sh
df -h | msgraph-sendmail --to admin@example.com --subject "Disk usage"
```

### Common options

| Option | Description |
|---|---|
| `--from ADDR` / `-f ADDR` | Sender mailbox (overrides `From:` header and config) |
| `--config FILE` | Use an alternate config file |
| `--save-to-sent` / `--no-save-to-sent` | Keep a copy in Sent Items (default: keep) |
| `--max-time SECONDS` | Give up (exit 75) after this long in total, retries included; default 50, `0` = no limit |
| `--dry-run` | Print the Graph API payload instead of sending |
| `--verbose` | Debug output on stderr |

### Which mailbox does it send from?

Precedence: `-f`/`--from` → the message's `From:` header → `from` in the
config file. Local addresses such as cron's `From: root (Cron Daemon)` or
`-f www-data` are never used as the sender — such mail goes out from
`from`. The mailbox must exist in your tenant; with the plain
`Mail.Send` application permission the app may send as **any** mailbox, so
consider an
[application access policy](docs/azure-setup.md#restrict-the-app-to-specific-mailboxes-recommended).

### Mail for root and other local users

System daemons write to `root`, cron to the job's owner, scripts to
`user@localhost` — addresses Exchange Online knows nothing about.
msgraph-sendmail resolves them like an MTA: the `[aliases]` section of the
config, then `/etc/aliases`, then the catch-all `local_recipient`:

```ini
[msgraph]
local_recipient = ops@contoso.com

[aliases]
www-data = webdev@contoso.com
```

A local address that resolves to nothing fails with exit code 67 rather
than vanishing. Details: [docs/configuration.md](docs/configuration.md#local-addresses).

### Time limits

The whole run — reading the message, getting a token, every retry and
pause — stays within `max_time` (default 50 s), so it fits the budget
that cron jobs, systemd units or backup hooks give sendmail. Set it a few
seconds below your callers' limit. On expiry, or if the caller kills it
with SIGTERM, the tool exits 75 and leaves a syslog line saying where it
stopped. Details:
[docs/configuration.md](docs/configuration.md#time-limits-and-retries).

## Exit codes

Standard BSD `sysexits(3)`, so cron and MTAs interpret failures correctly:

| Code | Meaning |
|---|---|
| 0 | Message accepted by Microsoft Graph |
| 64 | Command-line usage error |
| 65 | Message invalid (unparseable, no recipients, too large, rejected) |
| 67 | Local recipient (e.g. `root`) with no alias and no `local_recipient` |
| 69 | Service refused (authentication, missing permission, unknown mailbox) |
| 75 | Temporary failure — worth retrying (network, throttling, 5xx, `max_time` reached, killed by a signal) |
| 78 | Configuration missing or broken |

Errors are also logged to syslog (`mail` facility) so they surface in
`/var/log/mail.log` even when stderr is discarded.

## Limits and caveats

- Total request size is limited to **4 MiB** by the Graph `sendMail`
  endpoint (roughly 3 MB of attachments after Base64 overhead). Larger
  attachments would require Graph upload sessions, which this tool does not
  implement. It fails fast with a clear message instead.
- The sending mailbox must be licensed (or be a shared mailbox).
- This is a *submission* tool, not a queueing MTA: if Graph is unreachable
  it retries within `max_time` and then exits 75. Callers that need
  guaranteed delivery should retry on their side (e.g. `Restart=on-failure`
  on the systemd unit that sends an alert).
- A retry after a lost response may deliver a message twice; this is
  preferred over losing it and is logged.
- Applications inside containers can't use the host's
  `/usr/sbin/sendmail`; add msgraph-sendmail to the image instead — see
  [Containers](docs/sendmail-integration.md#containers-docker-compose-kubernetes)
  for a Dockerfile and Compose example.

## Development

```sh
python3 -m unittest discover -s tests -v
```

No dependencies, no build step. Pull requests welcome — please keep the
stdlib-only rule and add tests for behaviour changes.

## License

[MIT](LICENSE)
