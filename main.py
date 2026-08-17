"""
Card Connect — a Telegram bot that turns a name-card photo into a saved
Google Contact and a ready-to-send WhatsApp greeting.

Forward or send a photo of a business/name card and Card Connect will read
the key details (name, company, role, phone, email), ask you to confirm,
save it as a Google Contact on your linked account, and hand back a
click-to-chat WhatsApp link with a starter greeting pre-filled.

Run:
    export TELEGRAM_BOT_TOKEN="..."
    export FREELLMAPI_API_KEY="..."
    python authorize_google.py   # one-time, links your Google account
    python main.py
"""

import base64
import io
import json
import logging
import os
import re
import uuid
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import google_contacts

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
FREELLMAPI_API_KEY = os.environ["FREELLMAPI_API_KEY"]

# Self-hosted FreeLLMAPI instance (https://github.com/tashfeenahmed/freellmapi),
# an OpenAI-compatible router over free-tier quota from multiple providers.
FREELLMAPI_BASE_URL = os.environ.get("FREELLMAPI_BASE_URL", "http://localhost:3001/v1")

# "auto" lets the router's fallback chain pick a vision-capable free-tier
# model; pin this to a specific model id if you want deterministic behaviour.
MODEL = os.environ.get("FREELLMAPI_MODEL", "auto")

# Most vision-capable free-tier models top out around this edge length;
# anything bigger just burns tokens without adding OCR accuracy.
MAX_EDGE_PX = 1568

# Keep the base64 payload comfortably under typical free-tier upload caps.
MAX_UPLOAD_MB = 20

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("card-connect")

# httpx logs full request URLs at INFO, and Telegram's API puts the bot
# token in the URL path — silence it to keep the token out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

llm_client = OpenAI(api_key=FREELLMAPI_API_KEY, base_url=FREELLMAPI_BASE_URL)

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
  "address": string
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

CARD_INTROS = [
    "Reading the card... 🔍",
    "Scanning for the details~",
    "One sec, extracting the contact info...",
    "Give me a moment to read this card properly...",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prep_image(raw: bytes) -> tuple[bytes, str]:
    """Downscale large images and normalise to JPEG for a cheap, fast call."""
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE_PX:
        scale = MAX_EDGE_PX / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def _run_extraction(image_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:{media_type};base64,{b64}"

    response = llm_client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the contact details from this name card."},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
    )

    text = (response.choices[0].message.content or "").strip()
    # Vision models occasionally wrap JSON in fences despite instructions.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def _whatsapp_link(phone: str, name: str) -> str | None:
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) < 8:
        return None
    greeting = f"Hi {name.split()[0]}, great meeting you — this is Julian!" if name else "Hi, great meeting you!"
    return f"https://wa.me/{digits}?text={quote(greeting)}"


def _format_card_summary(card: dict) -> str:
    lines = [f"📇 {card.get('full_name', 'Unknown name')}"]
    if card.get("title") or card.get("organization"):
        role = " — ".join(x for x in [card.get("title"), card.get("organization")] if x)
        lines.append(role)
    for phone in card.get("phones", []):
        lines.append(f"📞 {phone}")
    for email in card.get("emails", []):
        lines.append(f"✉️ {email}")
    if card.get("website"):
        lines.append(f"🌐 {card['website']}")
    if card.get("address"):
        lines.append(f"📍 {card['address']}")
    return "\n".join(lines)


def _escape_markdown_v2(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi, I'm Card Connect! 📇\n\n"
        "Send me a photo of a business/name card — or forward one from another "
        "chat — and I'll:\n"
        "1️⃣ Read the name, company, role, phone, and email off it\n"
        "2️⃣ Ask you to confirm before saving anything\n"
        "3️⃣ Save it as a Google Contact on your linked account\n"
        "4️⃣ Hand you a WhatsApp link with a starter greeting pre-filled — "
        "you tap it to actually send\n\n"
        "Nothing is saved or sent until you confirm. /cancel discards a "
        "pending card at any time."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("pending_cards", None)
    await update.message.reply_text("Cleared. Send another card whenever you're ready.")


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat_id = message.chat_id

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        size_mb = (message.document.file_size or 0) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            await message.reply_text(
                f"That file's a bit big ({size_mb:.1f}MB). Try sending it under {MAX_UPLOAD_MB}MB."
            )
            return
        file_id = message.document.file_id

    if not file_id:
        return

    import random

    await message.reply_text(random.choice(CARD_INTROS))
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        tg_file = await context.bot.get_file(file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        image_bytes, media_type = _prep_image(raw)
        card = _run_extraction(image_bytes, media_type)
    except json.JSONDecodeError:
        logger.exception("Extraction returned non-JSON for chat %s", chat_id)
        await message.reply_text("I couldn't make sense of what I read off that card — mind trying a clearer photo?")
        return
    except Exception:
        logger.exception("Extraction failed for chat %s", chat_id)
        await message.reply_text("Something went wrong reading that image — mind trying again?")
        return

    if card.get("error") == "NOT_A_NAMECARD":
        await message.reply_text(
            "I couldn't find a business/name card in that image — send a clearer photo of one?"
        )
        return

    if not card.get("full_name"):
        await message.reply_text(
            "I could read the card but couldn't make out a name on it — mind sending a clearer photo?"
        )
        return

    token = uuid.uuid4().hex[:8]
    context.chat_data.setdefault("pending_cards", {})[token] = card

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Save contact", callback_data=f"save:{token}"),
                InlineKeyboardButton("❌ Discard", callback_data=f"discard:{token}"),
            ]
        ]
    )

    summary = _format_card_summary(card)
    await message.reply_text(
        f"Here's what I read:\n\n{summary}\n\nSave this as a Google Contact and get a WhatsApp link?",
        reply_markup=keyboard,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, _, token = query.data.partition(":")
    pending = context.chat_data.get("pending_cards", {})
    card = pending.pop(token, None)

    if card is None:
        await query.edit_message_text("This card's no longer pending — send a fresh photo to try again.")
        return

    if action == "discard":
        await query.edit_message_text("Discarded — nothing was saved.")
        return

    if action != "save":
        return

    try:
        google_contacts.create_contact(card)
    except Exception as exc:
        logger.exception("Failed to create Google Contact")
        await query.edit_message_text(
            f"Couldn't save the contact — {exc}\n\n"
            "If this is your first run, make sure you've completed `authorize_google.py` "
            "and set GOOGLE_TOKEN_JSON."
        )
        return

    name_escaped = _escape_markdown_v2(card.get("full_name", ""))
    reply_lines = [f"✅ Saved *{name_escaped}* to Google Contacts\\."]

    wa_link = None
    for phone in card.get("phones", []):
        wa_link = _whatsapp_link(phone, card.get("full_name", ""))
        if wa_link:
            break

    if wa_link:
        reply_lines.append(f"\n💬 [Tap to send a WhatsApp hi]({wa_link})")
    else:
        reply_lines.append("\n_No usable phone number found for a WhatsApp link\\._")

    await query.edit_message_text("\n".join(reply_lines), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Send me a photo of a business/name card and I'll take it from there! 📇")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.PHOTO & ~filters.Document.IMAGE, handle_other)
    )

    logger.info("Card Connect is online using model %s via %s", MODEL, FREELLMAPI_BASE_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
