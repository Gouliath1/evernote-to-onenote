# Registering an Azure app

The importer talks to OneNote as you, through the Microsoft Graph API. That needs an app registration: a free, three-minute step that gives you a client id. No secret is involved — sign-in happens in your browser and the app never sees your password.

## Steps

1. Go to the [Azure portal app registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) and choose **New registration**.
2. Give it any name, for example `evernote-to-onenote`.
3. **Supported account types** — this one matters:
   - Personal Microsoft account (outlook.com, hotmail.com, live.com): choose **Personal Microsoft accounts only**.
   - Work or school account: choose the single-tenant option.
4. Leave the redirect URI blank. **Register**.
5. Copy the **Application (client) ID** from the overview page.
6. Open **Authentication** → **Advanced settings** → set **Allow public client flows** to **Yes** → **Save**. Without this, the device-code sign-in is rejected.
7. Open **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions** → tick **Notes.ReadWrite** → **Add permissions**.

Then, in your working directory:

```bash
echo 'YOUR-APPLICATION-CLIENT-ID' > .client_id
```

Or set `ONENOTE_CLIENT_ID` in your environment instead.

## Signing in

The first command that touches OneNote prints a short code and a URL. Open the URL, enter the code, approve. The token is cached in `.msal_token.json` and refreshed automatically; you should not have to sign in again.

Keep `.client_id` and `.msal_token.json` out of version control. The bundled `.gitignore` already excludes them.

## If something goes wrong

**`AADSTS7000218` or the device-code request is rejected** — step 6 was missed. Allow public client flows.

**`40001` "The request does not contain a valid authentication token"** — the token lacks the OneNote scope. Check step 7, then delete `.msal_token.json` and sign in again so a fresh token is issued with the new permission.

**Graph Explorer shows 401 for `/me/onenote/notebooks`** — this is not a reliable test of your setup. Graph Explorer's permission panel often fails to load for personal accounts, and it cannot consent to the OneNote scopes. Test with this project's own sign-in instead.

**You have been told the OneNote API does not work for personal Microsoft accounts** — it does. This project was built and tested against a personal account. The reports behind that claim are usually a missing scope, mistaken for an account-type restriction.

**`/me` returns 401 but `/me/onenote/notebooks` works** — expected. We request `Notes.ReadWrite` and not `User.Read`, so reading your profile is genuinely not permitted. Nothing here needs it.
