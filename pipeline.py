"""Sales pipeline stages, follow-up scheduling and message formatting.

Pure helpers: no database, network or Telegram imports. All formatters
return Telegram HTML.
"""

from datetime import datetime, timedelta, timezone
from html import escape

STAGES = ["new", "contacted", "meeting", "proposal", "won", "lost"]
ACTIVE_STAGES = ["new", "contacted", "meeting", "proposal"]

STAGE_LABEL = {
    "new": "🆕 New",
    "contacted": "💬 Contacted",
    "meeting": "🤝 Meeting",
    "proposal": "📄 Proposal",
    "won": "🏆 Won",
    "lost": "💤 Lost",
}

# Days until the next nudge once a contact enters a stage (None = pipeline closed)
FOLLOWUP_DAYS = {
    "new": 2,
    "contacted": 3,
    "meeting": 2,
    "proposal": 4,
    "won": None,
    "lost": None,
}

# Used by the Snooze button and as the re-nag interval after a reminder is sent
SNOOZE_DAYS = 3

# One-off drafts after a card is saved: a LinkedIn comment on day 3, and a
# low-pressure "quick update" email on day 14 if they still haven't replied.
LINKEDIN_NUDGE_DAYS = 3
SECOND_TOUCH_DAYS = 14
# The day-14 email only makes sense while there has been no real reply yet.
SECOND_TOUCH_STAGES = ["new", "contacted"]


# ── time helpers ─────────────────────────────────────────────────────────────

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC ISO timestamp at second precision (sorts correctly as text)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def in_days(days: int) -> str:
    return iso(now() + timedelta(days=days))


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def followup_at(stage: str | None, days: int | None = None) -> str | None:
    """Timestamp for the next follow-up, or None if the stage is closed."""
    d = days if days is not None else FOLLOWUP_DAYS.get(stage or "new")
    if d is None:
        return None
    return in_days(d)


def stage_update_fields(stage: str) -> dict:
    """Columns to write when a contact moves to `stage`."""
    return {
        "stage": stage,
        "stage_updated_at": iso(now()),
        "next_followup_at": followup_at(stage),
    }


def new_contact_fields() -> dict:
    """Columns for a freshly saved card: stage 'new' plus the whole cadence."""
    return {
        **stage_update_fields("new"),
        "linkedin_nudge_at": in_days(LINKEDIN_NUDGE_DAYS),
        "second_touch_at": in_days(SECOND_TOUCH_DAYS),
    }


def describe_due(value) -> str | None:
    """Human-readable 'when is the next follow-up' string."""
    dt = parse_ts(value)
    if not dt:
        return None
    secs = (dt - now()).total_seconds()
    if secs <= 0:
        return "due now"
    if secs < 86400:
        return "within 24h"
    return f"in {round(secs / 86400)}d"


def _ago(dt: datetime) -> str:
    days = (now() - dt).days
    if days <= 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


# ── message formatters (HTML parse mode) ─────────────────────────────────────

def role_line(contact: dict) -> str:
    return " — ".join(p for p in (contact.get("title"), contact.get("organization")) if p)


def format_card_summary(card: dict) -> str:
    """What was read off the card, shown before anything is saved."""
    lines = [f"📇 <b>{escape(card.get('full_name') or 'Unknown name')}</b>"]
    if role_line(card):
        lines.append(escape(role_line(card)))
    lines += [f"📞 {escape(p)}" for p in card.get("phones") or []]
    lines += [f"✉️ {escape(e)}" for e in card.get("emails") or []]
    if card.get("website"):
        lines.append(f"🌐 {escape(card['website'])}")
    if card.get("address"):
        lines.append(f"📍 {escape(card['address'])}")
    if card.get("linkedin"):
        lines.append(f"🔗 {escape(card['linkedin'])}")
    return "\n".join(lines)


def format_contact(contact: dict) -> str:
    """Full saved-contact card: details, notes, pipeline state, intel summary."""
    lines = [format_card_summary(contact)]

    for label, key in (("Talked about", "topic"), ("They asked for", "request"), ("Notes", "notes")):
        if contact.get(key):
            lines.append(f"📝 <b>{label}:</b> {escape(contact[key])}")
    if contact.get("is_vip"):
        lines.append("🌟 VIP")

    stage = contact.get("stage") or "new"
    lines += ["", f"📊 Stage: {STAGE_LABEL.get(stage, escape(stage))}"]
    due = describe_due(contact.get("next_followup_at")) if stage in ACTIVE_STAGES else None
    if due:
        lines.append(f"⏰ Next follow-up: {due}")

    intel = contact.get("intel") or {}
    pains = [p.get("pain") for p in intel.get("pain_points") or [] if isinstance(p, dict) and p.get("pain")]
    if pains:
        lines += ["", "🎯 <b>Pain points</b>"] + [f"  • {escape(p)}" for p in pains[:3]]

    if contact.get("id") is not None:
        lines += ["", f"🆔 <code>{contact['id']}</code>"]
    return "\n".join(lines)


def format_reminder(contact: dict) -> str:
    """Follow-up card: who, where they are in the pipeline, what to say."""
    name = escape(contact.get("full_name") or "Unknown")
    stage = contact.get("stage") or "new"

    lines = [f"⏰ <b>Follow up: {name}</b>"]
    if role_line(contact):
        lines.append(escape(role_line(contact)))

    meta = [STAGE_LABEL.get(stage, stage)]
    scanned = parse_ts(contact.get("created_at"))
    if scanned:
        meta.append(f"scanned {_ago(scanned)}")
    changed = parse_ts(contact.get("stage_updated_at"))
    if changed and (not scanned or abs((changed - scanned).total_seconds()) > 60):
        meta.append(f"last update {_ago(changed)}")
    lines.append(" · ".join(escape(m) for m in meta))

    reach = (contact.get("phones") or [])[:1] + (contact.get("emails") or [])[:1]
    if reach:
        lines.append("📞 " + "  ·  ".join(escape(r) for r in reach))

    if contact.get("topic"):
        lines.append(f"📝 {escape(contact['topic'])}")

    pains = (contact.get("intel") or {}).get("pain_points") or []
    top = pains[0] if pains and isinstance(pains[0], dict) else {}
    if top.get("pain"):
        lines += ["", f"🎯 <b>{escape(top['pain'])}</b>"]
        if top.get("opportunity"):
            lines.append(f"💡 {escape(top['opportunity'])}")

    if contact.get("followup_opener"):
        lines += ["", "✉️ <b>Suggested opener</b>", f"<i>{escape(contact['followup_opener'])}</i>"]

    return "\n".join(lines)


def format_pipeline(contacts: list[dict]) -> str:
    """Stage-by-stage overview with overdue follow-ups flagged."""
    by_stage: dict[str, list[dict]] = {s: [] for s in STAGES}
    for c in contacts:
        by_stage[c.get("stage") if c.get("stage") in STAGES else "new"].append(c)

    current = now()
    overdue = 0
    lines = ["📊 <b>Pipeline</b>", ""]

    for s in STAGES:
        items = by_stage[s]
        lines.append(f"{STAGE_LABEL[s]} — <b>{len(items)}</b>")
        if s not in ACTIVE_STAGES:
            continue
        for i, c in enumerate(items):
            due = parse_ts(c.get("next_followup_at"))
            is_due = bool(due and due <= current)
            overdue += is_due
            if i < 5:
                org = f" ({escape(c['organization'])})" if c.get("organization") else ""
                lines.append(f"   • {escape(c.get('full_name') or 'Unknown')}{org}{' ⏰' if is_due else ''}")
        if len(items) > 5:
            lines.append(f"   …and {len(items) - 5} more")

    if overdue:
        lines += ["", f"⏰ {overdue} follow-up{'s' if overdue != 1 else ''} due — /followups"]
    return "\n".join(lines)
