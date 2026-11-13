"""Name-card photo -> structured contact details."""

import io

from PIL import Image

import llm

# Vision models gain nothing past this edge length; anything bigger just
# costs tokens and upload size (the gateway caps images at 5 MB).
MAX_EDGE_PX = 1568

EXTRACT_SYSTEM_PROMPT = """You are the extraction engine behind a Telegram bot called Card Connect.
You are given a photo of a business/name card. Extract the key contact
details and return ONLY a single JSON object — no markdown fences, no
commentary, no preamble.

Schema (omit a key entirely if that field isn't present on the card):
{
  "full_name": string,
  "organization": string,
  "title": string,          // job title / trade / role, e.g. "Sales Manager", "Electrician"
  "phones": [string, ...],  // as printed, digits/+ only, one per number
  "emails": [string, ...],
  "website": string,
  "address": string,
  "linkedin": string        // LinkedIn URL or handle, only if printed on the card
}

Rules:
- Only include a field if it is actually printed on the card. Do not guess
  or invent values.
- Normalise phone numbers to include a country code if one is printed or
  clearly implied by context (e.g. a local format next to a country flag or
  address); otherwise keep the digits as printed with a leading "+" only if
  a "+" or "00" prefix appears on the card.
- If the image is not a business/name card, or contains no legible contact
  details at all, output exactly:
  {"error": "NOT_A_NAMECARD"}
- Output raw JSON only. Never wrap it in markdown code fences or quotes.
"""

_TEXT_FIELDS = ("full_name", "organization", "title", "website", "address", "linkedin")
_LIST_FIELDS = ("phones", "emails")


class NotANameCard(Exception):
    """The image isn't a name card, or nothing on it was legible."""


def prep_image(raw: bytes) -> tuple[bytes, str]:
    """Downscale large images and normalise to JPEG for a cheap, fast call."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE_PX:
        scale = MAX_EDGE_PX / longest
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def normalise_card(data: dict) -> dict:
    """Coerce the model's JSON into the shape the rest of the bot relies on:
    text fields are non-empty strings, phones/emails are lists of strings,
    and anything else is dropped."""
    card: dict = {}
    for field in _TEXT_FIELDS:
        value = data.get(field)
        if isinstance(value, (str, int, float)) and str(value).strip():
            card[field] = str(value).strip()
    for field in _LIST_FIELDS:
        value = data.get(field)
        if isinstance(value, (str, int, float)):
            value = [value]
        if isinstance(value, list):
            items = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
            if items:
                card[field] = items
    return card


async def extract_card(raw_image: bytes) -> dict:
    """Read a name card. Raises NotANameCard, ValueError (unparseable reply)
    or llm.LLMError (gateway problem)."""
    image_bytes, media_type = prep_image(raw_image)
    reply = await llm.vision(
        llm.USE_CASE_EXTRACT,
        EXTRACT_SYSTEM_PROMPT,
        "Extract the contact details from this name card.",
        image_bytes,
        media_type,
        max_tokens=1024,
    )
    data = llm.parse_json(reply)
    if data.get("error"):
        raise NotANameCard()
    card = normalise_card(data)
    if not card.get("full_name"):
        raise NotANameCard()
    return card
