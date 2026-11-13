"""Background research on a freshly saved contact.

1. Optional: one Google search via Serper.dev (SERPER_API_KEY) for the
   person's public LinkedIn result and snippets.
2. The gateway turns card + snippets into current positions, three likely
   pain points and a ready-to-send opener.

Without a Serper key the model reasons from the card alone and says so
(confidence "low", data_source "namecard_only"). LinkedIn pages themselves
are never fetched: LinkedIn blocks anonymous requests, so it only added
dependencies and a terms-of-service problem.
"""

import json
import logging
import os
from html import escape

import httpx

import llm

logger = logging.getLogger("card-connect.intel")

SERPER_URL = "https://google.serper.dev/search"

SYNTHESIS_SYSTEM = """You are a sharp B2B sales intelligence analyst working for Julian, a Singapore
solopreneur. Given the details from someone's name card, Julian's own notes
about the conversation (if any) and Google search snippets about that person
(if any), extract and infer professional intelligence about them.

Return ONLY a raw JSON object — no markdown, no preamble:
{
  "linkedin_url": "a linkedin.com/in/ URL that appears in the input, or null",
  "current_positions": [
    {"title": "...", "company": "...", "duration": "... (only if stated)"}
  ],
  "pain_points": [
    {
      "pain": "Concise label of the pain point",
      "detail": "2-3 sentence explanation of WHY this is a pain for them in their role/industry",
      "opportunity": "How Julian could specifically address this"
    }
  ],
  "follow_up_opener": "A ready-to-send first follow-up message (2-3 sentences) from Julian to this person",
  "confidence": "high | medium | low",
  "data_source": "search_snippets | namecard_only"
}

Rules:
- Never invent facts about the person. Positions come only from the card or
  the snippets. linkedin_url must be copied from the input, otherwise null.
- follow_up_opener: warm, natural Singapore business tone for WhatsApp/LinkedIn. Reference having met and
  swapped name cards, tie in the single most relevant pain point (or what was actually discussed, if notes
  are given), offer something useful, and end with a soft, low-pressure ask. No hard sell, no bullet points,
  no [placeholders] — write it ready to send.
- List max 3 current positions.
- Always produce exactly 3 pain points — if data is thin, reason from their role/industry.
- Pain points must be SPECIFIC to their context, not generic platitudes. They are informed guesses: word
  them as likely, not as known facts.
- Opportunities must reference Julian's actual services: Accurova (premium photography/digital media),
  TheBooleanJulian (AI automation, bots, software), Educare4u (tutoring/education frameworks).
- With no search snippets, set data_source to "namecard_only" and confidence to "low".
"""


async def search_person(contact: dict) -> tuple[str | None, str]:
    """Google the person via Serper. Returns (linkedin_url_or_None, snippets);
    ("", "") style empties when no key is set or the search fails."""
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    name = contact.get("full_name") or ""
    if not api_key or not name:
        return None, ""

    query = f'"{name}"'
    if contact.get("organization"):
        query += f' "{contact["organization"]}"'
    query += " linkedin"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                SERPER_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 5},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("Serper search failed (%s)", type(exc).__name__)
        return None, ""

    return parse_search_results(data)


def parse_search_results(data: dict) -> tuple[str | None, str]:
    """Pick the first linkedin.com/in/ result and collect result snippets."""
    linkedin_url = None
    snippets = []
    for result in (data.get("organic") or [])[:5]:
        link = str(result.get("link") or "")
        if linkedin_url is None and "linkedin.com/in/" in link:
            linkedin_url = link.split("?")[0]
        parts = [str(result.get(k) or "") for k in ("title", "snippet")]
        if any(parts):
            snippets.append(f"{link}\n" + " — ".join(p for p in parts if p))
    return linkedin_url, "\n\n".join(snippets)


def build_context(contact: dict, linkedin_url: str | None, snippets: str) -> str:
    lines = ["NAME CARD:", f"Name: {contact.get('full_name') or 'Unknown'}"]
    for label, key in (("Title", "title"), ("Company", "organization"), ("Website", "website"), ("Address", "address")):
        if contact.get(key):
            lines.append(f"{label}: {contact[key]}")
    if contact.get("emails"):
        lines.append(f"Email: {contact['emails'][0]}")

    notes = [(label, contact.get(key)) for label, key in (("Talked about", "topic"), ("They asked for", "request"), ("Notes", "notes"))]
    if any(value for _, value in notes):
        lines += ["", "JULIAN'S NOTES:"] + [f"{label}: {value}" for label, value in notes if value]

    known_url = linkedin_url or contact.get("linkedin")
    if known_url:
        lines += ["", f"LINKEDIN: {known_url}"]
    if snippets:
        lines += ["", "GOOGLE SEARCH RESULTS:", snippets[:6000]]
    else:
        lines += ["", "No search results available — use the name card only."]
    return "\n".join(lines)


async def enrich(contact: dict) -> dict:
    """Search, then synthesise. Raises llm.LLMError / ValueError on failure."""
    linkedin_url, snippets = await search_person(contact)
    reply = await llm.complete(
        llm.USE_CASE_INTEL,
        SYNTHESIS_SYSTEM,
        build_context(contact, linkedin_url, snippets),
        max_tokens=1800,
        json_mode=True,
    )
    result = llm.parse_json(reply)
    # Trust the search result over the model for the URL; never keep one the
    # model produced with nothing to copy it from.
    result["linkedin_url"] = linkedin_url or (result.get("linkedin_url") if snippets else None)
    if not snippets:
        result["data_source"] = "namecard_only"
        result["confidence"] = "low"
    return result


def format_intel(result: dict, contact_name: str) -> str:
    """Telegram HTML message for a contact's intel."""
    lines = [f"🔍 <b>Intel — {escape(contact_name)}</b>"]

    confidence = str(result.get("confidence") or "unknown")
    dot = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪")
    source = {
        "search_snippets": "from public search results",
        "namecard_only": "inferred from the card alone",
    }.get(result.get("data_source"), "")
    lines.append(f"{dot} Confidence: <b>{escape(confidence)}</b>" + (f" — {source}" if source else ""))

    positions = [p for p in result.get("current_positions") or [] if isinstance(p, dict)]
    if positions:
        lines += ["", "💼 <b>Current positions</b>"]
        for pos in positions[:3]:
            entry = "  • " + escape(str(pos.get("title") or ""))
            if pos.get("company"):
                entry += f" @ {escape(str(pos['company']))}"
            if pos.get("duration"):
                entry += f" <i>{escape(str(pos['duration']))}</i>"
            lines.append(entry)

    pains = [p for p in result.get("pain_points") or [] if isinstance(p, dict) and p.get("pain")]
    if pains:
        lines += ["", "🎯 <b>Likely pain points &amp; openings</b>"]
        for i, pain in enumerate(pains[:3], 1):
            lines += ["", f"<b>{i}. {escape(str(pain['pain']))}</b>"]
            if pain.get("detail"):
                lines.append(f"<i>{escape(str(pain['detail']))}</i>")
            if pain.get("opportunity"):
                lines.append(f"💡 {escape(str(pain['opportunity']))}")

    if result.get("follow_up_opener"):
        lines += ["", "✉️ <b>Suggested opener</b>", f"<i>{escape(str(result['follow_up_opener']))}</i>"]

    url = str(result.get("linkedin_url") or "")
    if url.startswith("https://"):
        lines += ["", f'🔗 <a href="{escape(url, quote=True)}">LinkedIn profile</a>']

    return "\n".join(lines)


if __name__ == "__main__":  # quick manual check: python intel.py "Name" "Company"
    import asyncio
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    person = {"full_name": sys.argv[1], "organization": sys.argv[2] if len(sys.argv) > 2 else ""}
    print(json.dumps(asyncio.run(enrich(person)), indent=2, ensure_ascii=False))
