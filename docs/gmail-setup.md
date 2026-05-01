# Gmail API setup

em-phi uses the Gmail API via OAuth2. You need a Google Cloud project with the Gmail API enabled and an OAuth2 client credentials file. This is a one-time setup.

---

## 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Give it a name (e.g. `em-phi`) and click **Create**
4. Make sure the new project is selected in the dropdown

---

## 2. Enable the Gmail API

1. In the left menu go to **APIs & Services** → **Library**
2. Search for **Gmail API**
3. Click it, then click **Enable**

---

## 3. Configure the OAuth consent screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Select **External** and click **Create**
3. Fill in the required fields:
   - App name: `em-phi` (or anything you like)
   - User support email: your Gmail address
   - Developer contact email: your Gmail address
4. Click **Save and Continue** through the remaining steps (you can skip optional fields)
5. On the **Test users** step, click **Add users** and add your Gmail address
6. Click **Save and Continue**, then **Back to Dashboard**

> The app stays in "Testing" mode, which is fine for personal use. You do not need to publish it.

---

## 4. Create OAuth2 credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `em-phi` (or anything)
5. Click **Create**
6. Click **Download JSON** on the confirmation dialog

Save the downloaded file as `credentials.json` in the same directory as your `config.yaml` (or wherever your config points with `credentials_file`).

---

## 5. Generate token.json

This is a one-time step that opens a browser for the Google consent screen and writes `token.json`. After this, em-phi uses the token silently and auto-refreshes it — no browser needed again.

Save the following as `authorize.py` (anywhere convenient — you can delete it afterwards):

```python
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path
import sys

credentials_file = sys.argv[1] if len(sys.argv) > 1 else "credentials.json"
token_file = sys.argv[2] if len(sys.argv) > 2 else "token.json"

flow = InstalledAppFlow.from_client_secrets_file(
    credentials_file,
    scopes=["https://www.googleapis.com/auth/gmail.modify"],
)
creds = flow.run_local_server(port=0)
Path(token_file).write_text(creds.to_json())
print(f"Token saved to {token_file}")
```

Run it with the em-phi virtualenv (which already has the required library):

```bash
.venv/bin/python authorize.py credentials.json token.json
```

Place the resulting `token.json` wherever your `config.yaml` points with `email_provider.token_file`.

---

## Revoking access

To revoke em-phi's access to your Gmail account:

1. Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
2. Find the em-phi app and click **Remove access**

Delete `token.json` locally to complete the cleanup.

---

## Troubleshooting

**"The OAuth client was not found"** — make sure you downloaded the credentials for the correct project and that the Gmail API is enabled in that project.

**"Access blocked: em-phi has not completed Google's verification process"** — click **Advanced** → **Go to em-phi (unsafe)** on the consent screen. This is expected for personal apps in testing mode.

**Token expired / "invalid_grant"** — delete `token.json` and run `em-phi setup` again.
