"""
One-time helper: link a Google account to Card Connect (optional).

Run this locally (it needs a browser) once, sign in with the Google account
you want new contacts saved to, and it writes token.json next to this
script. Give that file's contents to the Worker as the GOOGLE_TOKEN_JSON
secret:

    npx wrangler secret put GOOGLE_TOKEN_JSON < token.json

Prerequisite: a Google Cloud project with the "People API" enabled and an
OAuth client ID of type "Desktop app", downloaded as credentials.json into
this same folder. See README.md for the click-by-click steps.

Run:
    pip install -r requirements.txt
    python authorize_google.py
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/contacts"]
CLIENT_SECRETS_PATH = "credentials.json"
TOKEN_PATH = Path("token.json")


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Saved credentials to {TOKEN_PATH.resolve()}")
    print("Now run: npx wrangler secret put GOOGLE_TOKEN_JSON < token.json")


if __name__ == "__main__":
    main()
