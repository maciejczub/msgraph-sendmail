# Configuration reference

## File locations

The first existing source wins:

1. `--config FILE` command-line option
2. `$MSGRAPH_SENDMAIL_CONFIG` environment variable
3. `~/.config/msgraph-sendmail.conf` (per user)
4. `/etc/msgraph-sendmail.conf` (system-wide)

Environment variables (below) override values from the file.

## File format

INI format, section `[msgraph]` (write `%%` for a literal `%`):

```ini
[msgraph]
tenant = contoso.com
client_id = 00000000-0000-0000-0000-000000000000
client_secret = xxx
#client_secret_file = /etc/msgraph-sendmail.secret
from = noreply@contoso.com
save_to_sent = true
timeout = 15
max_time = 50
```

| Key | Required | Description |
|---|---|---|
| `tenant` | yes | Tenant ID (GUID) or any verified domain of the tenant |
| `client_id` | yes | Application (client) ID of the app registration |
| `client_secret` | yes* | Client secret **value** |
| `client_secret_file` | yes* | Path to a file containing only the secret; used when `client_secret` is not set. Keep it `chmod 600 root:root`. |
| `from` | no | Default sender mailbox, used when neither `-f`/`--from` nor a `From:` header provides one |
| `save_to_sent` | no | Keep a copy in Sent Items (`true`/`false`, default `true`) |
| `timeout` | no | Network timeout in seconds for each socket operation (connect, send, wait for a response); default `15` |
| `max_time` | no | Time budget in seconds for the whole run, including retries; default `50`, `0` = no limit — see [Time limits and retries](#time-limits-and-retries) |
| `local_recipient` | no | Catch-all mailbox for local addresses without an alias — see [Local addresses](#local-addresses) |
| `aliases_file` | no | aliases(5) file to consult (default `/etc/aliases`; empty value disables) |
| `local_domains` | no | Comma-separated domains that mean "this host" (default: automatic) |

\* one of `client_secret` / `client_secret_file` is required.

If the file contains `client_secret` and is readable by group or others,
the tool prints a warning to stderr suggesting `chmod 600`.

## Environment variables

| Variable | Overrides |
|---|---|
| `MSGRAPH_SENDMAIL_CONFIG` | config file path |
| `MSGRAPH_SENDMAIL_TENANT` | `tenant` |
| `MSGRAPH_SENDMAIL_CLIENT_ID` | `client_id` |
| `MSGRAPH_SENDMAIL_CLIENT_SECRET` | `client_secret` |
| `MSGRAPH_SENDMAIL_CLIENT_SECRET_FILE` | `client_secret_file` |
| `MSGRAPH_SENDMAIL_FROM` | `from` |
| `MSGRAPH_SENDMAIL_LOCAL_RECIPIENT` | `local_recipient` |
| `MSGRAPH_SENDMAIL_ALIASES_FILE` | `aliases_file` |
| `MSGRAPH_SENDMAIL_MAX_TIME` | `max_time` (the `--max-time` option overrides both) |

Environment variables are handy in containers and CI:

```sh
export MSGRAPH_SENDMAIL_TENANT=contoso.com
export MSGRAPH_SENDMAIL_CLIENT_ID=...
export MSGRAPH_SENDMAIL_CLIENT_SECRET_FILE=/run/secrets/graph_mailer
echo "hi" | msgraph-sendmail --to ops@contoso.com --subject test
```

## Local addresses

System software mails *local* addresses: cron writes to the job owner
(`root`, `deploy`), mdadm and smartd to `root`, others to
`root@localhost` or `www-data@<hostname>`. None of these exist in
Exchange Online, so msgraph-sendmail maps them to real mailboxes the way
an MTA would.

An address is **local** when it has no `@`, or its domain is one of
`local_domains`. By default that is `localhost`, `localhost.localdomain`,
the host name and the host's FQDN (compared case-insensitively, ignoring a
trailing dot; internationalized names are not converted, so list both the
Unicode and the `xn--` form if you use them) — but never the domain of
`from`, so a
server whose FQDN equals your mail domain doesn't swallow real mail. Set
`local_domains` explicitly to replace the defaults (an empty value means
only `@`-less addresses are local). Every other address is left alone.

**Recipients** (To/Cc/Bcc, positional arguments, `-t`, `--to/--cc/--bcc`)
that are local are resolved in this order:

1. the `[aliases]` section of the config file,
2. the system aliases file (`aliases_file`, default `/etc/aliases`),
3. `local_recipient`, the catch-all for any local name without an alias.

Aliases expand recursively (`postmaster: root`, `root: admin`, …); a name
pointing back to itself or into a loop means "the local mailbox" and ends
at `local_recipient`. Nesting deeper than 100 levels is a configuration
error (exit 78), never a silent fallback to the catch-all. Targets are separated by commas;
comments such as `ops@contoso.com (on call)` are ignored. `/dev/null`
discards the mail (exit 0, nothing sent if no other recipient remains).
Pipe (`|cmd`), file (`/path`) and `:include:` targets cannot be delivered
and are skipped; an alias with *only* such targets counts as unresolvable
and does **not** fall back to `local_recipient`.

Because the aliases file decides where `root`'s mail goes, it must be a
regular file of at most 1 MiB, owned by root (or the invoking user) and
not writable by group or others; otherwise — and if it exists but cannot
be read — the tool exits with code 78 instead of guessing. A missing file
is simply skipped.

```ini
[msgraph]
from = noreply@contoso.com
local_recipient = ops@contoso.com

[aliases]
root = ops@contoso.com, boss@contoso.com
www-data = webdev@contoso.com
```

If a local recipient resolves to nothing (no alias, no `local_recipient`),
the message is **not** sent and the tool exits with code 67 (`EX_NOUSER`)
naming the address. A local `Reply-To:` that cannot be resolved is
dropped instead.

Check your setup without sending anything (no credentials needed):

```console
$ msgraph-sendmail -bv root www-data alice@contoso.com
root... deliverable: ops@contoso.com, boss@contoso.com
www-data... deliverable: webdev@contoso.com
alice@contoso.com... deliverable: alice@contoso.com
```

Rewrites are shown with `--verbose`, and the syslog line of a sent message
gets an `orig_to=` field listing the original local addresses.

## Sender resolution

The mailbox used for `POST /users/{mailbox}/sendMail` is picked in this
order:

1. `-f ADDR` / `-r ADDR` / `--from ADDR`
2. the message's `From:` header (sendmail mode)
3. `from` in the configuration

Local addresses (see above) are skipped, because they can never be a
Graph mailbox: cron's `From: root (Cron Daemon)` therefore ends up being
sent from `from`. A local `-f`/`--from` (e.g. `-f www-data`) marks system
mail and goes straight to `from` — the `From:` header is not consulted
then. Aliases are not applied to senders.

Remember that with the plain `Mail.Send` application permission anyone
who can run the tool with your credentials can send as any mailbox via
`-f` or `From:`; restrict the app with an
[application access policy](azure-setup.md#restrict-the-app-to-specific-mailboxes-recommended).

If no usable sender remains the tool exits with code 78 (configuration
error).

## Time limits and retries

Programs that call sendmail usually give it a time budget — a systemd
unit's timeout, a backup script's alert hook, PHP's execution limit. Two
settings keep msgraph-sendmail inside it:

- **`max_time`** (default 50 s; `--max-time`, `MSGRAPH_SENDMAIL_MAX_TIME`)
  bounds the whole run, measured from start-up: reading the message from
  stdin, the token request, every `sendMail` attempt and every pause
  between attempts (and `-bv` / `--dry-run` output). Set it a few
  seconds *below* your caller's limit. When it runs out, the tool exits
  with code 75 and logs why (stage, attempt, elapsed time) to syslog.
  `0` disables it.
- **`timeout`** (default 15 s) bounds each individual network operation.
  Every request gets `min(timeout, time left)`, so a slow server cannot
  push the run past `max_time`; a real-time timer enforces the deadline
  even against a server that trickles bytes. (One exception: a DNS lookup
  stuck inside the system resolver can't always be interrupted — the
  resolver's own timeout from `/etc/resolv.conf` applies.)

Within that budget, failures are retried up to 4 attempts per request,
with pauses of 2, 4 and 8 seconds:

| Failure | Retried? |
|---|---|
| HTTP 429 / 5xx from Graph or Entra ID | yes; a `Retry-After` of up to 30 s is honoured, a longer one ends the run at once (exit 75) |
| Network error before the request was sent (DNS, connection refused, TLS handshake) | yes |
| Network error after the request was sent (connection reset, no response in time) | yes — but Graph may already have accepted the message, so it **may arrive twice**; a warning saying so goes to syslog |
| TLS certificate verification failure | no (exit 75) |
| Other HTTP errors (bad credentials, missing permission, unknown mailbox, invalid message) | no (exit 69 / 65) |

Each retry is logged to syslog at warning level, so slow or flaky
delivery is visible even when it eventually succeeds.

If the caller kills the tool (SIGTERM, SIGHUP, SIGINT or SIGALRM), it logs
for example
`aborted by SIGTERM after 12.3s (stage: send, attempt 2; the message may or may not have been accepted)`
and exits 75 — or exits 0 if Graph had already accepted the message, so
the caller doesn't send it again. SIGKILL cannot be caught — give the
tool a chance with SIGTERM first (systemd does by default).

A `202 Accepted` from Graph is final: the tool never retries after it.
HTTP redirects are not followed (Graph and Entra ID don't use them here).

There is no queue: after exit 75 nothing is retried later unless the
caller does it.

## Troubleshooting

Run with `--verbose` for debug output, or `--dry-run` to inspect the exact
Graph payload without sending. Errors also go to syslog (`mail` facility).

| Symptom | Likely cause |
|---|---|
| `authentication failed (HTTP 401) ... AADSTS7000215` | Wrong `client_secret` (did you paste the secret *ID* instead of the *value*?) |
| `authentication failed (HTTP 400) ... AADSTS700016` | Wrong `client_id` or wrong `tenant` |
| `HTTP 403 ErrorAccessDenied` | `Mail.Send` application permission missing or admin consent not granted; or an application access policy blocks this mailbox |
| `mailbox ... not found` (404) | Sender mailbox doesn't exist or has no Exchange license |
| exit code 75 | Network problem, Graph throttling/outage or `max_time` reached — safe to retry; syslog says which and at what stage |
| exit code 67, `no mailbox for local address(es)` | Mail to `root`/a Unix user with no alias — add it to `[aliases]` or set `local_recipient`; test with `-bv` |
| message arrives without custom headers | Graph only forwards custom headers that start with `X-` |
