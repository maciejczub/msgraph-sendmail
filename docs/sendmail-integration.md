# Using msgraph-sendmail as the system sendmail

The point of this tool is that programs which shell out to
`/usr/sbin/sendmail` keep working after your organization turns off SMTP
submission. Here is how to wire it up.

## Prerequisites

- Working config in `/etc/msgraph-sendmail.conf` including a `from`
  address (system daemons usually don't set a usable `From:` header) and
  a `local_recipient` (or `[aliases]`) for mail addressed to `root` and
  other local users — see [Local addresses](configuration.md#local-addresses).
- No other MTA fighting for the path: remove or stop postfix/exim/ssmtp
  if they own `/usr/sbin/sendmail`, or use the alternatives system below.

Verify first:

```sh
echo "test body" | msgraph-sendmail --to you@example.com --subject "pre-flight"
msgraph-sendmail -bv root www-data      # where does local mail go?
```

## Option A: plain symlink

```sh
install -m 755 msgraph_sendmail.py /usr/local/bin/msgraph-sendmail
ln -s /usr/local/bin/msgraph-sendmail /usr/sbin/sendmail
ln -s /usr/local/bin/msgraph-sendmail /usr/lib/sendmail   # some tools use this path
```

## Option B: Debian/Ubuntu alternatives (survives package operations better)

```sh
update-alternatives --install /usr/sbin/sendmail sendmail \
    /usr/local/bin/msgraph-sendmail 100
```

## Verifying

```sh
printf 'To: you@example.com\nSubject: sendmail path test\n\nIt works.\n' \
    | /usr/sbin/sendmail -t
echo "exit: $?"    # 0 = accepted by Graph
tail -f /var/log/mail.log   # success/failure is logged to the mail facility
```

## PHP

`mail()` uses `sendmail_path` from `php.ini` — the default
(`/usr/sbin/sendmail -t -i`) works as-is with the symlink. To be explicit:

```ini
sendmail_path = /usr/local/bin/msgraph-sendmail -t -i
```

Mailer libraries that default to `sendmail -bs` (SMTP over stdin, e.g.
Symfony Mailer's `SendmailTransport`) must be switched to `-t -i`;
msgraph-sendmail rejects `-bs` with exit code 64 instead of guessing.

Note that PHP's fifth `mail()` parameter (`additional_params`) often passes
`-f sender@domain`; that is honoured and selects the sending mailbox.

## Cron

Cron pipes job output to `sendmail -i -FCronDaemon -oi -t` (cronie) or
`sendmail -FCronDaemon -i -B8BITMIME -oem <user>` (Debian/Ubuntu) — all of
these flags are understood or safely ignored. By default the mail goes to
the job owner (`root`, `deploy`, …), which is resolved through
`[aliases]`, `/etc/aliases` and `local_recipient`. You can also set the
recipient per crontab:

```cron
MAILTO=admin@example.com
0 3 * * * /usr/local/bin/backup.sh
```

Cron's `From: root (Cron Daemon)` is a local address, so the `from` value
in `/etc/msgraph-sendmail.conf` is used as the sending mailbox.

## Applications that speak SMTP only

This tool replaces the *sendmail binary*, not an SMTP server. If an
application can only submit via SMTP (`localhost:25`), you still need a
bridge; consider keeping a local postfix instance that pipes to
msgraph-sendmail via a `pipe` transport:

```
# /etc/postfix/master.cf
graph  unix  -  n  n  -  -  pipe
  flags=F user=nobody argv=/usr/local/bin/msgraph-sendmail -f ${sender} ${recipient}

# /etc/postfix/main.cf
default_transport = graph
```

(That said, if you control the application, calling msgraph-sendmail
directly is simpler and has fewer moving parts.)

## Containers (Docker, Compose, Kubernetes)

An application in a container doesn't see the host's `/usr/sbin/sendmail`,
and images such as the official `php:*` ones ship no sendmail at all —
PHP's `mail()` simply fails there. Put msgraph-sendmail into the image:
it is one file plus a Python interpreter.

### Dockerfile

Debian-based images (`php:*-apache`, `php:*-fpm`, `php:*-cli`, …):

```dockerfile
# syntax=docker/dockerfile:1
FROM php:8.4-apache

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:a3869d6840f0a45ed231636450b5f491b84ddc59ceb3ec35c50a96a220acd18f \
    --chmod=755 \
    https://github.com/maciejczub/msgraph-sendmail/releases/download/v1.1.0/msgraph_sendmail.py \
    /usr/local/bin/msgraph-sendmail
RUN ln -sf /usr/local/bin/msgraph-sendmail /usr/sbin/sendmail
```

Alpine-based images (`php:*-alpine`): replace the `apt-get` line with
`RUN apk add --no-cache python3`.

- Install `python3`, **not** `python3-minimal`: the minimal Debian package
  lacks modules msgraph-sendmail needs (`http`, among others). Python adds
  about 35 MB to a Debian image and 40 MB to an Alpine one.
- The checksum pins the exact release file; it is listed in the release
  notes. When upgrading, change the version in the URL and the checksum
  together.
- PHP's default `sendmail_path` (`/usr/sbin/sendmail -t -i`) then works
  unchanged — no `php.ini` edits.

### Configuration and the secret

Keep both out of the image and mount them at runtime. A config file works
with every PHP setup:

```yaml
# compose.yaml
services:
  app:
    build: .
    volumes:
      - ./msgraph-sendmail.conf:/etc/msgraph-sendmail.conf:ro
    secrets:
      - graph_client_secret

secrets:
  graph_client_secret:
    file: ./secrets/graph_client_secret   # contains only the secret value
```

```ini
# msgraph-sendmail.conf — no secret in here
[msgraph]
tenant = contoso.com
client_id = 00000000-0000-0000-0000-000000000000
client_secret_file = /run/secrets/graph_client_secret
from = noreply@contoso.com
local_recipient = ops@contoso.com
max_time = 20
```

- Both files must be readable by the user PHP runs as **inside** the
  container (`www-data`: uid/gid 33 in Debian-based images, 82 in Alpine
  ones). Plain Compose mounts secrets as files with their host owner and
  mode, so e.g. `chgrp 33 secrets/graph_client_secret && chmod 640
  secrets/graph_client_secret`. In Kubernetes, mount a Secret volume and
  set `defaultMode` / `fsGroup` accordingly.
- The `MSGRAPH_SENDMAIL_*` environment variables work as well, but
  PHP-FPM clears its workers' environment by default (`clear_env = yes`),
  so the sendmail process started by `mail()` would not see them. The
  config file avoids that pitfall.
- Check the result from inside the container (nothing is sent):
  `docker compose exec app sendmail -bv root www-data`.

### The application can read the secret

Whatever runs as the web server user can read the client secret — just
as it could read SMTP credentials in any application's config. With the
plain `Mail.Send` application permission that secret can send as **any**
mailbox in the tenant, so restrict the app registration to the one
sending mailbox with an
[application access policy](azure-setup.md#restrict-the-app-to-specific-mailboxes-recommended).
A leaked secret can then only send as that mailbox.

### Time budget and logs

- `mail()` blocks the web request until msgraph-sendmail exits. Keep
  `max_time` well below your web server's and proxy's timeouts (15–25 s is
  a reasonable range); it is also the longest a page can hang on mail.
- `mail()` returns `false` whenever msgraph-sendmail exits non-zero.
- Most containers have no syslog daemon. Error messages still go to
  stderr, which PHP passes on to the web server's error log — in the
  official images that ends up in `docker logs`.

### Why there is no host-side bridge

Keeping the secret on the host — a socket-activated service on the host
that containers hand their mail to — was considered and set aside
([#3](https://github.com/maciejczub/msgraph-sendmail/issues/3)).
It only fits one deployment shape (Docker on a systemd host, with access
to the compose file), adds a local mail relay that has to be secured and
maintained, and an application access policy already limits what a leaked
secret can do.

## Things to know

- **No queue.** If Graph is down, the tool retries within its time budget
  (`max_time`, default 50 s) and exits 75. Classic sendmail queues and
  retries for days. For cron/monitoring mail this rarely matters; for
  must-not-lose mail, keep a queueing MTA in front (see the postfix bridge
  above) or retry in the caller — e.g. `Restart=on-failure` on the systemd
  unit that sends the alert.
- **Time budget.** Set `max_time` a few seconds below the time your caller
  allows sendmail; see
  [Time limits and retries](configuration.md#time-limits-and-retries).
- **Envelope ≠ headers.** Recipients passed as arguments are used as the
  actual (To) recipients of the Graph message; with `-t` they are taken
  from `To:`/`Cc:`/`Bcc:` headers instead. `Bcc:` recipients are honoured
  and never appear in the delivered message.
- **Local delivery is mapped, not performed.** Mail for `root` and other
  local users is redirected to real mailboxes via `[aliases]`,
  `/etc/aliases` and `local_recipient`; pipe/file/`:include:` alias
  targets and `~/.forward` are not supported. A local recipient that
  resolves to nothing fails the message (exit 67) rather than being
  silently dropped.
- **Replacing an ad-hoc wrapper.** If you previously put a script in
  front of msgraph-sendmail to rewrite `root` (or similar), point
  `/usr/sbin/sendmail` straight at msgraph-sendmail and move the mapping
  into `[aliases]`/`local_recipient`.
