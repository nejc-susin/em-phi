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

## 5. Run the authorization flow

```bash
export ANTHROPIC_API_KEY=sk-ant-...
em-phi --config config.yaml setup
```

This opens a browser window for the Google consent screen. Sign in with the Gmail account you added as a test user. After approving, em-phi saves a refresh token to `token.json`. Subsequent runs use this token silently — no browser needed.

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
