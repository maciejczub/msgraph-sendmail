# Setting up Microsoft Entra ID for msgraph-sendmail

`msgraph-sendmail` authenticates as an *application* (OAuth 2.0 client
credentials flow) — no user account, no interactive login, no password
storage. This requires a one-time setup by a tenant administrator.

## 1. Register the application

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com)
   as an administrator.
2. Go to **Identity → Applications → App registrations → New registration**.
3. Name it (e.g. `linux-mailer`), leave **Supported account types** at
   *Accounts in this organizational directory only*, no redirect URI needed.
4. Click **Register**.
5. On the app's **Overview** page, note down:
   - **Application (client) ID** → `client_id`
   - **Directory (tenant) ID** → `tenant` (a verified domain like
     `contoso.com` works too)

## 2. Grant the Mail.Send application permission

1. In the app registration, open **API permissions → Add a permission**.
2. Choose **Microsoft Graph → Application permissions** (⚠️ *not*
   "Delegated permissions" — this tool runs without a signed-in user).
3. Search for and tick **`Mail.Send`**, then **Add permissions**.
4. Click **Grant admin consent for &lt;your tenant&gt;** and confirm.
   The Status column must show a green check mark
   ("Granted for &lt;tenant&gt;").

> **Symptom of skipping this step:** the tool exits with code 69 and
> `HTTP 403 ErrorAccessDenied`. You can verify what the token actually
> contains — its `roles` claim must include `Mail.Send`.

## 3. Create a client secret

1. In the app registration, open **Certificates & secrets → Client secrets
   → New client secret**.
2. Choose an expiry (set yourself a reminder — mail stops when it expires!).
3. Copy the **Value** column immediately (it is shown only once).
   ⚠️ Use the secret **Value**, *not* the "Secret ID" GUID.
4. Put it in the config file as `client_secret` (or in a separate root-only
   file referenced by `client_secret_file`).

## 4. Restrict the app to specific mailboxes (recommended)

By default, an app with the `Mail.Send` application permission can send as
**every mailbox in the tenant**. For a mail relay that only ever sends as
`noreply@contoso.com`, lock it down with an *application access policy*:

```powershell
# Exchange Online PowerShell (Connect-ExchangeOnline)

# 1. Create a mail-enabled security group and add the allowed mailboxes
New-DistributionGroup -Name "msgraph-sendmail-allowed" -Type Security `
    -Members noreply@contoso.com

# 2. Restrict the app to that group (use YOUR client_id)
New-ApplicationAccessPolicy -AppId 00000000-0000-0000-0000-000000000000 `
    -PolicyScopeGroupId msgraph-sendmail-allowed@contoso.com `
    -AccessRight RestrictAccess `
    -Description "msgraph-sendmail may only send as members of this group"

# 3. Verify (takes up to ~30 min to propagate)
Test-ApplicationAccessPolicy -AppId 00000000-0000-0000-0000-000000000000 `
    -Identity noreply@contoso.com
```

With the policy in place, attempts to send as any other mailbox fail with
`403 ErrorAccessDenied` — exactly what you want.

## 5. About the sending mailbox

- The `from` mailbox must actually exist in Exchange Online — a licensed
  user mailbox or a (free) **shared mailbox**. A bare alias/distribution
  address will yield `404` or `ErrorInvalidUser`.
- A shared mailbox is usually the best choice for automated mail: free,
  no password, no MFA to manage.
- Sent messages are stored in that mailbox's *Sent Items* folder unless you
  pass `--no-save-to-sent` or set `save_to_sent = false`.

## Secret hygiene

- `chmod 600` the config file; the tool warns when it is group/world
  readable.
- Client secrets expire (max 24 months). Track the expiry date; rotating is
  just "create new secret → update config → delete old secret".
- For higher assurance you can register a *certificate* instead of a secret;
  this tool currently supports secrets only — open an issue if you need
  certificate auth.
