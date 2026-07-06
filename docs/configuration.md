# Configuration reference

## File locations

The first existing source wins:

1. `--config FILE` command-line option
2. `$MSGRAPH_SENDMAIL_CONFIG` environment variable
3. `~/.config/msgraph-sendmail.conf` (per user)
4. `/etc/msgraph-sendmail.conf` (system-wide)

Environment variables (below) override values from the file.

## File format

INI format, section `[msgraph]`:

```ini
[msgraph]
tenant = contoso.com
client_id = 00000000-0000-0000-0000-000000000000
client_secret = xxx
#client_secret_file = /etc/msgraph-sendmail.secret
from = noreply@contoso.com
save_to_sent = true
timeout = 30
```

| Key | Required | Description |
|---|---|---|
| `tenant` | yes | Tenant ID (GUID) or any verified domain of the tenant |
| `client_id` | yes | Application (client) ID of the app registration |
| `client_secret` | yes* | Client secret **value** |
| `client_secret_file` | yes* | Path to a file containing only the secret; used when `client_secret` is not set. Keep it `chmod 600 root:root`. |
| `from` | no | Default sender mailbox, used when neither `-f`/`--from` nor a `From:` header provides one |
| `save_to_sent` | no | Keep a copy in Sent Items (`true`/`false`, default `true`) |
| `timeout` | no | HTTP timeout in seconds (default `30`) |

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

Environment variables are handy in containers and CI:

```sh
export MSGRAPH_SENDMAIL_TENANT=contoso.com
export MSGRAPH_SENDMAIL_CLIENT_ID=...
export MSGRAPH_SENDMAIL_CLIENT_SECRET_FILE=/run/secrets/graph_mailer
echo "hi" | msgraph-sendmail --to ops@contoso.com --subject test
```

## Sender resolution

The mailbox used for `POST /users/{mailbox}/sendMail` is picked in this
order:

1. `-f ADDR` / `-r ADDR` / `--from ADDR`
2. the message's `From:` header (sendmail mode)
3. `from` in the configuration

If none is available the tool exits with code 78 (configuration error).

## Troubleshooting

Run with `--verbose` for debug output, or `--dry-run` to inspect the exact
Graph payload without sending. Errors also go to syslog (`mail` facility).

| Symptom | Likely cause |
|---|---|
| `authentication failed (HTTP 401) ... AADSTS7000215` | Wrong `client_secret` (did you paste the secret *ID* instead of the *value*?) |
| `authentication failed (HTTP 400) ... AADSTS700016` | Wrong `client_id` or wrong `tenant` |
| `HTTP 403 ErrorAccessDenied` | `Mail.Send` application permission missing or admin consent not granted; or an application access policy blocks this mailbox |
| `mailbox ... not found` (404) | Sender mailbox doesn't exist or has no Exchange license |
| exit code 75 | Network problem or Graph throttling/outage — safe to retry |
| message arrives without custom headers | Graph only forwards custom headers that start with `X-` |
