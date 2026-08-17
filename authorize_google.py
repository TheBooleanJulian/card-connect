"""
One-time helper: link a Google account to Card Connect.

Run this locally (it needs a browser) once, sign in with the Google account
you want new contacts saved to, and it writes token.json next to this
script. Copy that file's contents into the GOOGLE_TOKEN_JSON env var when
deploying, or leave token.json in place for local runs.

Prerequisite: a Google Cloud project with the "People API" enabled and an
OAuth client ID of type "Desktop app", downloaded as credentials.json into
this same folder. See README.md for the click-by-click steps.

Run:
    python authorize_google.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

from google_contacts import SCOPES, TOKEN_PATH

CLIENT_SECRETS_PATH = "credentials.json"


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Saved credentials to {TOKEN_PATH.resolve()}")
    print("Copy this file's contents into GOOGLE_TOKEN_JSON for deployment, or keep it as-is for local runs.")


if __name__ == "__main__":
    main()
