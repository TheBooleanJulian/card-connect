"""
Google Contacts (People API) integration for Card Connect.

Uses a one-time OAuth flow (see authorize_google.py) to obtain a refresh
token for a single Google account, then creates contacts in that account
via the People API. Contacts created this way sync to any device signed
into that Google account (Android phone, Google Contacts web, etc.).
"""

import json
import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger("card-connect.google_contacts")

SCOPES = ["https://www.googleapis.com/auth/contacts"]

TOKEN_PATH = Path(os.environ.get("GOOGLE_TOKEN_PATH", "token.json"))


def _load_credentials() -> Credentials:
    """Load stored OAuth credentials, refreshing the access token if needed.

    Supports either a token.json file on disk (local dev) or the same JSON
    passed inline via the GOOGLE_TOKEN_JSON env var (for hosts like Zeabur
    where writing a persistent file is inconvenient).
    """
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if raw:
        info = json.loads(raw)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    else:
        raise RuntimeError(
            "No Google credentials found. Run `python authorize_google.py` once "
            "locally to link your Google account, then set GOOGLE_TOKEN_JSON "
            "(or keep token.json next to the bot) before starting it."
        )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist the refreshed access token so we don't re-refresh every call.
        if not raw:
            TOKEN_PATH.write_text(creds.to_json())

    return creds


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name.strip(), ""
    return " ".join(parts[:-1]), parts[-1]


def create_contact(card: dict) -> dict:
    """Create a Google Contact from extracted name-card data.

    `card` is expected to have: full_name, organization, title, phones
    (list[str]), emails (list[str]), website, address (all optional except
    full_name).

    Returns the created person resource from the People API.
    """
    creds = _load_credentials()
    service = build("people", "v1", credentials=creds)

    given, family = _split_name(card.get("full_name") or "")

    body: dict = {}

    if given or family:
        body["names"] = [{"givenName": given, "familyName": family}]

    if card.get("organization") or card.get("title"):
        body["organizations"] = [
            {
                "name": card.get("organization") or "",
                "title": card.get("title") or "",
            }
        ]

    phones = card.get("phones") or []
    if phones:
        body["phoneNumbers"] = [{"value": p} for p in phones]

    emails = card.get("emails") or []
    if emails:
        body["emailAddresses"] = [{"value": e} for e in emails]

    if card.get("website"):
        body["urls"] = [{"value": card["website"]}]

    if card.get("address"):
        body["addresses"] = [{"formattedValue": card["address"]}]

    body["biographies"] = [
        {"value": "Added via Card Connect (Telegram bot)", "contentType": "TEXT_PLAIN"}
    ]

    person = service.people().createContact(body=body).execute()
    logger.info("Created contact %s", person.get("resourceName"))
    return person
