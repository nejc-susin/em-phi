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