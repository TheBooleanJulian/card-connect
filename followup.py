"""Drafts the messages behind Card Connect's post-event follow-up cadence.

Every draft is handed back to the user to review and send themselves —
consistent with the rest of Card Connect, nothing here sends anything on
its own (see README > "Why a link instead of auto-sending WhatsApp").

Each function takes a saved contact (storage.get_contact) and reads the
meeting notes (topic / request / notes) off it.
"""

import llm

# ---------------------------------------------------------------------------
# Phase 2 — first follow-up email (within 24h), "3 C's" structure
# ---------------------------------------------------------------------------

_EMAIL_SYSTEM = """You draft short first-follow-up emails after a networking \
event, for a user named Julian. Follow the "3 C's" structure exactly:
- Context: remind them where/when you met (keep it brief, no fake specifics
  beyond what's given).
- Compliment: reference the one specific thing they said (proves you
  listened) — never a generic compliment.
- Call-to-Action: low friction. Never ask for coffee or a call outright.

Pick whichever fits best:
- "Value Drop" style: reference their stated problem/topic and offer a
  useful pointer, explicitly say no reply is needed.
- "Soft Connect" style: if their "request" implies they want an intro to
  someone or something, offer to make that intro if useful, no pressure.

Output plain text only: a "Subject: ..." line, a blank line, then the body.
No markdown, no commentary, no placeholders like [Name] — use the details
given or omit that part."""


async def draft_first_followup(contact: dict) -> str:
    return await _draft(_EMAIL_SYSTEM, contact, max_tokens=350)


# ---------------------------------------------------------------------------
# Phase 3 — LinkedIn comment prompt (day 3)
# ---------------------------------------------------------------------------

_LINKEDIN_SYSTEM = """You draft one thoughtful LinkedIn comment for a user \
named Julian to leave on a recent connection's post, a few days after \
meeting them at a networking event. The comment must NOT be generic praise
("Great post!", "Love this!"). It should reference the topic they discussed
in person and ask a specific, informed follow-up question about their work,
as if genuinely curious. One or two sentences max.

Output plain text only: the comment itself, nothing else."""


async def draft_linkedin_comment(contact: dict) -> str:
    return await _draft(_LINKEDIN_SYSTEM, contact, max_tokens=150, with_request=False)


# ---------------------------------------------------------------------------
# Phase 4 — second touch "update" email (day ~14, if no reply)
# ---------------------------------------------------------------------------

_UPDATE_SYSTEM = """You draft a short "second touch" follow-up email for a \
user named Julian to send to someone who hasn't replied to his first \
follow-up after a networking event, about two weeks later. It must NOT
sound like a "just circling back" nudge. Instead:
- Say he took their advice / acted on the topic discussed and got a small
  result (keep it plausible and modest, tied to the topic given).
- Thank them again, briefly.
- Explicitly say there's no pressure to reply.

Subject line format: "Quick update re: [short topic]".

Output plain text only: a "Subject: ..." line, a blank line, then the body.
No markdown, no commentary, no placeholders."""


async def draft_second_touch(contact: dict) -> str:
    return await _draft(_UPDATE_SYSTEM, contact, max_tokens=300, with_request=False)


# ---------------------------------------------------------------------------
# Phase 5 — VIP / gatekeeper note, the "3-2-1" rule
# ---------------------------------------------------------------------------

_VIP_SYSTEM = """You draft a short note for a user named Julian to send to \
a VIP or C-suite contact he just met at a networking event, meant either as
a handwritten note or a LinkedIn voice-note script. Follow the "3-2-1" rule
exactly:
- 3 sentences max.
- 2 specific references to the actual conversation (topic/request given).
- 1 specific ask or offer — never a vague "let's catch up".

Output plain text only: the note itself, nothing else. No greeting/signature
block beyond what naturally fits in the 3 sentences."""


async def draft_vip_note(contact: dict) -> str:
    return await _draft(_VIP_SYSTEM, contact, max_tokens=200)


# ---------------------------------------------------------------------------
# The "Ultimate Hack" — second-meeting ask, offered alongside the VIP note
# ---------------------------------------------------------------------------

_SECOND_MEETING_SYSTEM = """You draft one short message for a user named \
Julian asking a contact for a second meeting. Never frame it as "let's catch
up" or "grab coffee". Frame it as a specific request for their expertise —
e.g. "I'm deciding between A and B, could I get 10 minutes of your gut
reaction so I don't make a mistake?" — using the actual topic/request given
to build a real dilemma, not a generic one. One or two sentences max.

Output plain text only: the message itself, nothing else."""


async def draft_second_meeting_ask(contact: dict) -> str:
    return await _draft(_SECOND_MEETING_SYSTEM, contact, max_tokens=150)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _draft(system: str, contact: dict, max_tokens: int, with_request: bool = True) -> str:
    return await llm.complete(llm.USE_CASE_FOLLOWUP, system, contact_context(contact, with_request), max_tokens=max_tokens)


def contact_context(contact: dict, with_request: bool = True) -> str:
    lines = [f"Name: {contact.get('full_name') or 'Unknown'}"]
    role = " at ".join(p for p in (contact.get("title"), contact.get("organization")) if p)
    if role:
        lines.append(f"Role: {role}")
    if contact.get("topic"):
        lines.append(f"Topic discussed: {contact['topic']}")
    if with_request and contact.get("request"):
        lines.append(f"Their request: {contact['request']}")
    if contact.get("notes"):
        lines.append(f"Julian's notes: {contact['notes']}")
    return "\n".join(lines)
