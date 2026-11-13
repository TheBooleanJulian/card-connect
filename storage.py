"""SQLite store for Card Connect: the contact, what was discussed, where it
sits in the pipeline and when the next nudge is due.

Google Contacts (when linked) holds the address-book copy; this is the CRM
side that the People API has nowhere to put. Reminder timestamps live here
too, so a restart never drops a pending nudge.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pipeline

DB_PATH = Path(os.environ.get("CARD_CONNECT_DB_PATH", "card_connect.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    full_name TEXT,
    organization TEXT,
    title TEXT,
    phones TEXT,
    emails TEXT,
    website TEXT,
    address TEXT,
    topic TEXT,
    request TEXT,
    voice_file_id TEXT,
    is_vip INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# Columns added after v0.2.0. init_db() adds whichever are missing, so an
# existing card_connect.db keeps its rows.
_ADDED_COLUMNS = {
    "linkedin": "TEXT",
    "notes": "TEXT",
    "google_resource_name": "TEXT",
    "stage": "TEXT NOT NULL DEFAULT 'new'",
    "stage_updated_at": "TEXT",
    "next_followup_at": "TEXT",   # NULL = nothing scheduled / pipeline closed
    "linkedin_nudge_at": "TEXT",  # NULL once the day-3 draft has been sent
    "second_touch_at": "TEXT",    # NULL once the day-14 draft has been sent
    "followup_opener": "TEXT",
    "intel": "TEXT",              # JSON from intel.py
}

_JSON_COLUMNS = ("phones", "emails", "intel")
_CARD_COLUMNS = ("full_name", "organization", "title", "phones", "emails", "website", "address", "linkedin")
_UPDATABLE = {
    "topic", "request", "notes", "voice_file_id", "is_vip", "linkedin",
    "stage", "stage_updated_at", "next_followup_at", "linkedin_nudge_at",
    "second_touch_at", "followup_opener", "intel",
}
NUDGE_COLUMNS = ("linkedin_nudge_at", "second_touch_at")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)")}
        for column, declaration in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {column} {declaration}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_followup ON contacts (next_followup_at)")
        conn.commit()


def _encode(column: str, value):
    if column in _JSON_COLUMNS:
        return None if value is None else json.dumps(value)
    if column == "is_vip":
        return 1 if value else 0
    return value


def _decode(row: sqlite3.Row) -> dict:
    contact = dict(row)
    contact["phones"] = json.loads(contact["phones"] or "[]")
    contact["emails"] = json.loads(contact["emails"] or "[]")
    contact["intel"] = json.loads(contact["intel"]) if contact.get("intel") else None
    contact["is_vip"] = bool(contact["is_vip"])
    return contact


def save_contact(chat_id: int, card: dict, google_resource_name: str | None = None) -> int:
    """Insert a confirmed card as a new lead with its follow-up cadence
    scheduled, and return its row id."""
    values = {
        "chat_id": chat_id,
        "created_at": pipeline.iso(pipeline.now()),
        "google_resource_name": google_resource_name,
        **{c: _encode(c, card.get(c) or ([] if c in _JSON_COLUMNS else None)) for c in _CARD_COLUMNS},
        **pipeline.new_contact_fields(),
    }
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with closing(_connect()) as conn:
        cur = conn.execute(f"INSERT INTO contacts ({columns}) VALUES ({marks})", tuple(values.values()))
        conn.commit()
        return cur.lastrowid


def update_contact(contact_id: int, **fields) -> bool:
    """Set the given columns. Returns False if the contact doesn't exist."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if not fields:
        return False
    assignments = ", ".join(f"{column} = ?" for column in fields)
    params = [_encode(column, value) for column, value in fields.items()]
    with closing(_connect()) as conn:
        cur = conn.execute(f"UPDATE contacts SET {assignments} WHERE id = ?", (*params, contact_id))
        conn.commit()
        return cur.rowcount > 0


def get_contact(contact_id: int, chat_id: int | None = None) -> dict | None:
    """Fetch one contact; with chat_id, only if that chat saved it."""
    sql, params = "SELECT * FROM contacts WHERE id = ?", [contact_id]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    with closing(_connect()) as conn:
        row = conn.execute(sql, params).fetchone()
        return _decode(row) if row else None


def delete_contact(contact_id: int, chat_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM contacts WHERE id = ? AND chat_id = ?", (contact_id, chat_id))
        conn.commit()
        return cur.rowcount > 0


def _select(where: str, params: tuple, order: str, limit: int) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT * FROM contacts WHERE {where} ORDER BY {order} LIMIT ?", (*params, limit))
        return [_decode(row) for row in rows]


def list_contacts(chat_id: int, limit: int = 10) -> list[dict]:
    return _select("chat_id = ?", (chat_id,), "id DESC", limit)


def search_contacts(chat_id: int, query: str, limit: int = 20) -> list[dict]:
    """Substring match on name, organisation, email, phone and notes."""
    like = f"%{query}%"
    where = (
        "chat_id = ? AND (full_name LIKE ? OR organization LIKE ? OR emails LIKE ? "
        "OR phones LIKE ? OR notes LIKE ? OR topic LIKE ?)"
    )
    return _select(where, (chat_id, like, like, like, like, like, like), "id DESC", limit)


def get_pipeline(chat_id: int, limit: int = 500) -> list[dict]:
    return _select("chat_id = ?", (chat_id,), "stage_updated_at DESC", limit)


def get_due_followups(now_iso: str, chat_id: int | None = None, limit: int = 50) -> list[dict]:
    """Contacts in an open stage whose next follow-up has passed, oldest first."""
    marks = ", ".join("?" for _ in pipeline.ACTIVE_STAGES)
    where = f"stage IN ({marks}) AND next_followup_at IS NOT NULL AND next_followup_at <= ?"
    params = [*pipeline.ACTIVE_STAGES, now_iso]
    if chat_id is not None:
        where += " AND chat_id = ?"
        params.append(chat_id)
    return _select(where, tuple(params), "next_followup_at", limit)


def get_due_nudges(column: str, now_iso: str, limit: int = 50) -> list[dict]:
    """Contacts whose one-off day-3 / day-14 draft is due and not yet sent."""
    if column not in NUDGE_COLUMNS:
        raise ValueError(column)
    return _select(f"{column} IS NOT NULL AND {column} <= ?", (now_iso,), column, limit)
