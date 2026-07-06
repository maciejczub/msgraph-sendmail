# Using msgraph-sendmail as the system sendmail

The point of this tool is that programs which shell out to
`/usr/sbin/sendmail` keep working after your organization turns off SMTP
submission. Here is how to wire it up.

## Prerequisites

- Working config in `/etc/msgraph-sendmail.conf` including a `from`
  address (system daemons usually don't set a usable `From:` header).
- No other MTA fighting for the path: remove or stop postfix/exim/ssmtp
  if they own `/usr/sbin/sendmail`, or use the alternatives system below.

Verify first:

```sh
echo "test body" | msgraph-sendmail --to you@example.com --subject "pre-flight"
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

Note that PHP's fifth `mail()` parameter (`additional_params`) often passes
`-f sender@domain`; that is honoured and selects the sending mailbox.

## Cron

Cron pipes job output to `sendmail -i -FCronDaemon -oi -t` (flags vary by
implementation) — all of these are understood or safely ignored. Set the
recipient per crontab:

```cron
MAILTO=admin@example.com
0 3 * * * /usr/local/bin/backup.sh
```

Cron does not set a `From:` header address that Exchange would accept, so
the `from` value in `/etc/msgraph-sendmail.conf` is what will be used.

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

## Things to know

- **No queue.** If Graph is down, the tool retries briefly and exits 75.
  Classic sendmail queues and retries for days. For cron/monitoring mail
  this rarely matters; for must-not-lose mail, keep a queueing MTA in
  front (see the postfix bridge above) or handle retries in the caller.
- **Envelope ≠ headers.** Recipients passed as arguments are used as the
  actual (To) recipients of the Graph message; with `-t` they are taken
  from `To:`/`Cc:`/`Bcc:` headers instead. `Bcc:` recipients are honoured
  and never appear in the delivered message.
- **Aliases are not expanded.** `/etc/aliases` is not consulted; `root`
  is not a valid Graph recipient. Use full addresses (cron `MAILTO`,
  etc.).
