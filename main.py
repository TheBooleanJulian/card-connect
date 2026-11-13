"""
Card Connect — a Telegram bot that turns a name-card photo into a saved
contact, a WhatsApp greeting, background intel and a follow-up pipeline.

Send or forward a photo of a business/name card and Card Connect will read
the details, ask you to confirm, save the contact (to Google Contacts when
an account is linked, and always to its own SQLite CRM), research the
person, draft your follow-ups and remind you when each one is due.

Run:
    cp .env.example .env         # TELEGRAM_BOT_TOKEN, LLM_GATEWAY_KEY, ALLOWED_USER_IDS
    python authorize_google.py   # optional, one-time: links your Google account
    python main.py
"""

import asyncio
import logging
import os
import random
import re
import uuid
from collections import defaultdict
from datetime import time as dtime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)

import extraction
import followup
import google_contacts
import intel
import llm
import pipeline
import storage

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Telegram user IDs allowed to use the bot. Empty = nobody: the bot writes to
# a personal address book and spends LLM quota, so it is closed by default.
ALLOWED_USER_IDS = {
    int(uid) for uid in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if uid.strip().isdigit()
}

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Singapore"))
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "9"))
MAX_CARDS_PER_BATCH = 10  # follow-up cards sent per chat per run, to avoid flooding

# Telegram refuses bot downloads above 20 MB.
MAX_UPLOAD_MB = 20

TOPIC, REQUEST, VOICE, VIP = range(4)

HTML = ParseMode.HTML

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("card-connect")

# httpx logs full request URLs at INFO, and Telegram's API puts the bot
# token in the URL path — silence it to keep the token out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CARD_INTROS = [
    "Reading the card... 🔍",
    "Scanning for the details~",
    "One sec, extracting the contact info...",
    "Give me a moment to read this card properly...",
]

HELP_TEXT = (
    "Hi, I'm Card Connect! 📇\n\n"
    "Send me a photo of a business/name card — or forward one from another "
    "chat — and I'll:\n"
    "1️⃣ Read the name, company, role, phone and email off it\n"
    "2️⃣ Ask you to confirm before saving anything\n"
    "3️⃣ Save the contact and hand you a WhatsApp link with a greeting "
    "pre-filled — you tap it to actually send\n"
    "4️⃣ Research them and suggest pain points and an opener\n"
    "5️⃣ Draft your follow-ups and remind you when each is due\n\n"
    "<b>Commands</b>\n"
    "/pipeline — contacts by stage\n"
    "/followups — follow-ups that are due now\n"
    "/list — recent contacts\n"
    "/search &lt;text&gt; — find by name, company, email, phone or notes\n"
    "/show &lt;id&gt; — a contact's card and buttons\n"
    "/delete &lt;id&gt; — delete a contact\n"
    "/cancel — discard pending cards / stop the current questions\n\n"
    "Nothing is ever sent to your contacts automatically."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def whatsapp_link(phone: str, name: str) -> str | None:
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) < 8:
        return None
    greeting = f"Hi {name.split()[0]}, great meeting you — this is Julian!" if name else "Hi, great meeting you!"
    return f"https://wa.me/{digits}?text={quote(greeting)}"


def contact_keyboard(contact_id: int, stage: str | None) -> InlineKeyboardMarkup:
    """Stage buttons (current one ticked) + notes / snooze / delete."""
    stage = stage if stage in pipeline.STAGES else "new"
    stage_buttons = [
        InlineKeyboardButton(
            ("✓ " if s == stage else "") + pipeline.STAGE_LABEL[s],
            callback_data=f"stage:{contact_id}:{s}",
        )
        for s in pipeline.STAGES
    ]
    actions = [InlineKeyboardButton("✏️ Notes", callback_data=f"edit_notes:{contact_id}")]
    if stage in pipeline.ACTIVE_STAGES:
        actions.append(
            InlineKeyboardButton(f"😴 Snooze {pipeline.SNOOZE_DAYS}d", callback_data=f"snooze:{contact_id}")
        )
    actions.append(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{contact_id}"))
    return InlineKeyboardMarkup([stage_buttons[:3], stage_buttons[3:], actions])


def _contact_id_arg(text: str) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: only allow-listed users get any further."""
    user = update.effective_user
    if user and user.id in ALLOWED_USER_IDS:
        return
    if update.callback_query:
        await update.callback_query.answer("⛔ Not authorised.", show_alert=True)
    elif update.effective_message and user:
        await update.effective_message.reply_text(
            f"⛔ Not authorised. Your Telegram user ID is {user.id} — if this is your bot, "
            "add it to ALLOWED_USER_IDS and restart."
        )
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Card photo -> confirm
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT + f"\n\n⏰ Reminders arrive daily at {REMINDER_HOUR:02d}:00 ({TIMEZONE.key}).",
        parse_mode=HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("pending_cards", None)
    context.user_data.pop("awaiting_notes_for", None)
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

    await message.reply_text(random.choice(CARD_INTROS))
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        tg_file = await context.bot.get_file(file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        card = await extraction.extract_card(raw)
    except extraction.NotANameCard:
        await message.reply_text(
            "I couldn't find a name on a business/name card in that image — send a clearer photo of one?"
        )
        return
    except ValueError:
        logger.exception("Extraction returned non-JSON for chat %s", chat_id)
        await message.reply_text("I couldn't make sense of what I read off that card — mind trying a clearer photo?")
        return
    except llm.LLMError as exc:
        logger.error("Extraction failed for chat %s: %s", chat_id, exc)
        await message.reply_text(f"I couldn't reach the model to read that card ({exc}). Try again in a bit.")
        return
    except Exception:
        logger.exception("Extraction failed for chat %s", chat_id)
        await message.reply_text("Something went wrong reading that image — mind trying again?")
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
    await message.reply_text(
        f"Here's what I read:\n\n{pipeline.format_card_summary(card)}\n\nSave this contact?",
        parse_mode=HTML,
        reply_markup=keyboard,
    )


async def handle_discard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, _, token = query.data.partition(":")
    card = context.chat_data.get("pending_cards", {}).pop(token, None)

    if card is None:
        await query.edit_message_text("This card's no longer pending — send a fresh photo to try again.")
        return

    await query.edit_message_text("Discarded — nothing was saved.")


# ---------------------------------------------------------------------------
# Save -> meeting notes conversation -> first draft
# ---------------------------------------------------------------------------

_VIP_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Yes", callback_data="vip:yes"), InlineKeyboardButton("No", callback_data="vip:no")]]
)
_VOICE_PROMPT = "Optional: send a voice memo summarizing the vibe / key takeaway, or /skip."
_VIP_PROMPT = "Is this a VIP / high-profile contact? Changes the follow-up path."


async def save_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    _, _, token = query.data.partition(":")
    card = context.chat_data.get("pending_cards", {}).pop(token, None)

    if card is None:
        await query.edit_message_text("This card's no longer pending — send a fresh photo to try again.")
        return ConversationHandler.END

    chat_id = query.message.chat_id
    name = card.get("full_name", "")

    resource_name = None
    if not google_contacts.is_configured():
        saved_where = "Saved to the bot's CRM (Google Contacts isn't linked)."
    else:
        try:
            person = await asyncio.to_thread(google_contacts.create_contact, card)
            resource_name = person.get("resourceName")
            saved_where = "Saved to Google Contacts and the bot's CRM."
        except Exception:
            logger.exception("Failed to create Google Contact")
            saved_where = (
                "Saved to the bot's CRM. ⚠️ Google Contacts refused it — "
                "re-run authorize_google.py if the link has expired."
            )

    contact_id = storage.save_contact(chat_id, card, google_resource_name=resource_name)
    contact = storage.get_contact(contact_id)

    lines = [f"✅ <b>{escape(name)}</b> — {saved_where}"]
    wa_link = next(filter(None, (whatsapp_link(p, name) for p in card.get("phones", []))), None)
    if wa_link:
        lines.append(f'\n💬 <a href="{escape(wa_link, quote=True)}">Tap to send a WhatsApp hi</a>')
    else:
        lines.append("\n<i>No usable phone number found for a WhatsApp link.</i>")
    await query.edit_message_text("\n".join(lines), parse_mode=HTML, disable_web_page_preview=True)

    await context.bot.send_message(
        chat_id=chat_id,
        text=pipeline.format_contact(contact),
        parse_mode=HTML,
        reply_markup=contact_keyboard(contact_id, contact["stage"]),
        disable_web_page_preview=True,
    )

    # Research runs in the background and posts its own message when done.
    context.application.create_task(send_intel(context.bot, chat_id, contact), update=update)

    context.chat_data["active_followup"] = contact_id
    await context.bot.send_message(
        chat_id=chat_id,
        text="Quick log for smarter follow-ups — what did you two talk about? (or /skip)",
    )
    return TOPIC


async def send_intel(bot, chat_id: int, contact: dict) -> None:
    """Research a saved contact, store the result and post it to the chat."""
    name = contact.get("full_name") or "this contact"
    try:
        result = await intel.enrich(contact)
    except Exception as exc:
        logger.error("Intel failed for contact %s: %s", contact["id"], exc)
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Couldn't research {name} right now.")
        return

    updates = {"intel": result}
    if result.get("follow_up_opener"):
        updates["followup_opener"] = str(result["follow_up_opener"])
    if result.get("linkedin_url") and not contact.get("linkedin"):
        updates["linkedin"] = result["linkedin_url"]
    storage.update_contact(contact["id"], **updates)

    await bot.send_message(
        chat_id=chat_id,
        text=intel.format_intel(result, name),
        parse_mode=HTML,
        disable_web_page_preview=True,
    )


async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact_id = context.chat_data.get("active_followup")
    if contact_id:
        storage.update_contact(contact_id, topic=update.message.text.strip())
    await update.message.reply_text("Got it. What did they ask for / need from you? (or /skip)")
    return REQUEST


async def skip_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("No worries. What did they ask for / need from you? (or /skip)")
    return REQUEST


async def receive_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact_id = context.chat_data.get("active_followup")
    if contact_id:
        storage.update_contact(contact_id, request=update.message.text.strip())
    await update.message.reply_text(_VOICE_PROMPT)
    return VOICE


async def skip_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(_VOICE_PROMPT)
    return VOICE


async def receive_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact_id = context.chat_data.get("active_followup")
    if contact_id:
        storage.update_contact(contact_id, voice_file_id=update.message.voice.file_id)
    await update.message.reply_text(_VIP_PROMPT, reply_markup=_VIP_KEYBOARD)
    return VIP


async def skip_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(_VIP_PROMPT, reply_markup=_VIP_KEYBOARD)
    return VIP


async def receive_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    is_vip = query.data == "vip:yes"
    contact_id = context.chat_data.pop("active_followup", None)
    if contact_id:
        storage.update_contact(contact_id, is_vip=is_vip)
    contact = storage.get_contact(contact_id) if contact_id else None

    if contact is None:
        await query.edit_message_text("Lost track of that follow-up — send a fresh card to try again.")
        return ConversationHandler.END

    await query.edit_message_text("VIP contact noted 🌟" if is_vip else "Got it — standard follow-up path.")

    chat_id = query.message.chat_id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await send_first_draft(context.bot, chat_id, contact)
    return ConversationHandler.END


async def cancel_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data.pop("active_followup", None)
    await update.message.reply_text("Okay, skipping the rest of the follow-up log for this contact.")
    return ConversationHandler.END


async def send_first_draft(bot, chat_id: int, contact: dict) -> None:
    name = escape(contact.get("full_name") or "them")
    try:
        if contact["is_vip"]:
            note = await followup.draft_vip_note(contact)
            ask = await followup.draft_second_meeting_ask(contact)
            text = (
                f"🌟 <b>VIP path for {name}</b>\n\n"
                "Skip email — send this as a handwritten note or a LinkedIn "
                "voice-note script (3-2-1 rule):\n\n"
                f"{escape(note)}\n\n"
                "When you're ready to ask for a second meeting, use something like:\n\n"
                f"{escape(ask)}"
            )
        else:
            email = await followup.draft_first_followup(contact)
            text = f"📧 <b>Follow-up draft for {name}</b> (send within 24h):\n\n{escape(email)}"
    except Exception as exc:
        logger.error("Failed to draft follow-up for contact %s: %s", contact["id"], exc)
        text = "Saved your notes, but couldn't draft the follow-up message right now."
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML)


# ---------------------------------------------------------------------------
# Reminders: pipeline follow-ups + the day-3 / day-14 drafts
# ---------------------------------------------------------------------------


async def send_followup_cards(bot, chat_id: int, contacts: list[dict], reschedule: bool) -> None:
    """Send a header plus one actionable card per contact.

    reschedule=True (the daily run) pushes each contact's next follow-up out
    by SNOOZE_DAYS so an ignored reminder comes back later instead of daily.
    """
    total = len(contacts)
    shown = contacts[:MAX_CARDS_PER_BATCH]

    await bot.send_message(
        chat_id=chat_id,
        text=f"⏰ <b>{total} follow-up{'s' if total != 1 else ''} due</b>",
        parse_mode=HTML,
    )
    for c in shown:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=pipeline.format_reminder(c),
                parse_mode=HTML,
                reply_markup=contact_keyboard(c["id"], c.get("stage")),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.error("Could not send follow-up card for %s: %s", c["id"], exc)
            continue
        if reschedule:
            storage.update_contact(c["id"], next_followup_at=pipeline.in_days(pipeline.SNOOZE_DAYS))

    if total > len(shown):
        await bot.send_message(
            chat_id=chat_id,
            text=f"…and {total - len(shown)} more. They'll come up in the next run, or use /followups.",
        )


async def send_due_nudge(bot, contact: dict, column: str) -> None:
    """Send one day-3 (LinkedIn comment) or day-14 (second-touch email) draft.

    The timestamp is cleared once the draft is sent — or straight away when
    the contact has moved past the point where it applies. If drafting or
    sending fails it stays set, so the next run retries.
    """
    name = escape(contact.get("full_name") or "them")
    stage = contact.get("stage") or "new"

    if column == "linkedin_nudge_at":
        applies = stage in pipeline.ACTIVE_STAGES
    else:
        applies = stage in pipeline.SECOND_TOUCH_STAGES

    if applies:
        if column == "linkedin_nudge_at":
            comment = await followup.draft_linkedin_comment(contact)
            text = (
                f"👀 <b>Day {pipeline.LINKEDIN_NUDGE_DAYS} nudge — {name}</b>\n\n"
                "Visit their LinkedIn, watch/read a recent post first, then comment something like:\n\n"
                f"{escape(comment)}"
            )
        else:
            email = await followup.draft_second_touch(contact)
            text = (
                f"🔁 <b>Day {pipeline.SECOND_TOUCH_DAYS} check-in — {name}</b>\n\n"
                'No reply yet? Send this instead of "just circling back":\n\n'
                f"{escape(email)}"
            )
        await bot.send_message(chat_id=contact["chat_id"], text=text, parse_mode=HTML, disable_web_page_preview=True)

    storage.update_contact(contact["id"], **{column: None})


async def run_reminders(bot) -> None:
    """Everything that is due right now, for every chat. Driven entirely by
    timestamps in the database, so a run missed while the bot was off is
    simply picked up by the next one."""
    now_iso = pipeline.iso(pipeline.now())

    by_chat: dict[int, list[dict]] = defaultdict(list)
    for contact in storage.get_due_followups(now_iso):
        by_chat[contact["chat_id"]].append(contact)
    for chat_id, contacts in by_chat.items():
        try:
            await send_followup_cards(bot, chat_id, contacts, reschedule=True)
        except Exception as exc:
            logger.error("Reminder batch failed for chat %s: %s", chat_id, exc)

    nudges = 0
    for column in storage.NUDGE_COLUMNS:
        for contact in storage.get_due_nudges(column, now_iso):
            try:
                await send_due_nudge(bot, contact, column)
                nudges += 1
            except Exception as exc:
                logger.error("%s for contact %s failed, will retry: %s", column, contact["id"], exc)

    logger.info("Reminder run: %d follow-up(s), %d draft(s)", sum(map(len, by_chat.values())), nudges)


async def reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_reminders(context.bot)


# ---------------------------------------------------------------------------
# CRM commands and contact buttons
# ---------------------------------------------------------------------------


async def followups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    due = storage.get_due_followups(pipeline.iso(pipeline.now()), chat_id=chat_id)
    if not due:
        await update.message.reply_text("🎉 No follow-ups due right now.")
        return
    await send_followup_cards(context.bot, chat_id, due, reschedule=False)


async def pipeline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contacts = storage.get_pipeline(update.effective_chat.id)
    if not contacts:
        await update.message.reply_text("📭 No contacts yet — send me a name card.")
        return
    await update.message.reply_text(pipeline.format_pipeline(contacts), parse_mode=HTML)


def _contact_lines(contacts: list[dict]) -> list[str]:
    lines = []
    for c in contacts:
        org = f" — {escape(c['organization'])}" if c.get("organization") else ""
        stage = pipeline.STAGE_LABEL.get(c.get("stage"), "")
        lines.append(f"• <b>{escape(c.get('full_name') or 'Unknown')}</b>{org} {stage} <code>{c['id']}</code>")
    return lines


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contacts = storage.list_contacts(update.effective_chat.id, limit=10)
    if not contacts:
        await update.message.reply_text("📭 No contacts yet — send me a name card.")
        return
    lines = [f"📋 <b>Recent contacts</b> ({len(contacts)} shown) — /show &lt;id&gt; for details\n"]
    await update.message.reply_text("\n".join(lines + _contact_lines(contacts)), parse_mode=HTML)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Usage: /search <name, company, email, phone or note text>")
        return
    results = storage.search_contacts(update.effective_chat.id, query)
    if not results:
        await update.message.reply_text(f"🔎 No results for “{query}”.")
        return
    lines = [f"🔎 <b>Results for “{escape(query)}”</b> — /show &lt;id&gt; for details\n"]
    await update.message.reply_text("\n".join(lines + _contact_lines(results)), parse_mode=HTML)


async def show_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact_id = _contact_id_arg(" ".join(context.args or []))
    contact = storage.get_contact(contact_id, update.effective_chat.id) if contact_id is not None else None
    if contact is None:
        await update.message.reply_text("Usage: /show <id> — the id is the number shown by /list and /search.")
        return
    await update.message.reply_text(
        pipeline.format_contact(contact),
        parse_mode=HTML,
        reply_markup=contact_keyboard(contact["id"], contact.get("stage")),
        disable_web_page_preview=True,
    )


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact_id = _contact_id_arg(" ".join(context.args or []))
    if contact_id is None:
        await update.message.reply_text("Usage: /delete <id>")
        return
    if storage.delete_contact(contact_id, update.effective_chat.id):
        await update.message.reply_text(
            f"🗑 Contact {contact_id} deleted from the bot's CRM. (A Google Contact, if one was saved, is untouched.)"
        )
    else:
        await update.message.reply_text("❌ No contact with that id.")


async def contact_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """stage:<id>:<stage> | snooze:<id> | delete:<id> | edit_notes:<id>"""
    query = update.callback_query
    action, _, rest = (query.data or "").partition(":")
    id_text, _, stage = rest.partition(":")
    chat_id = query.message.chat_id

    contact_id = _contact_id_arg(id_text)
    contact = storage.get_contact(contact_id, chat_id) if contact_id is not None else None
    if contact is None:
        await query.answer("That contact no longer exists.", show_alert=True)
        return

    if action == "stage":
        if stage not in pipeline.STAGES:
            await query.answer("Unknown stage.")
            return
        storage.update_contact(contact_id, **pipeline.stage_update_fields(stage))
        days = pipeline.FOLLOWUP_DAYS.get(stage)
        await query.answer(
            pipeline.STAGE_LABEL[stage] + (f" — next follow-up in {days}d" if days else " — pipeline closed")
        )
        try:
            await query.edit_message_reply_markup(reply_markup=contact_keyboard(contact_id, stage))
        except BadRequest:
            pass  # message unchanged (same stage tapped again)

    elif action == "snooze":
        storage.update_contact(contact_id, next_followup_at=pipeline.in_days(pipeline.SNOOZE_DAYS))
        await query.answer(f"😴 Snoozed {pipeline.SNOOZE_DAYS} days")

    elif action == "delete":
        storage.delete_contact(contact_id, chat_id)
        await query.answer()
        await query.edit_message_text("🗑 Contact deleted from the bot's CRM.")

    elif action == "edit_notes":
        context.user_data["awaiting_notes_for"] = contact_id
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✏️ Send me the note to add for {contact.get('full_name') or 'this contact'}… (/cancel to stop)",
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact_id = context.user_data.pop("awaiting_notes_for", None)
    contact = storage.get_contact(contact_id, update.effective_chat.id) if contact_id else None
    if contact is None:
        await update.message.reply_text("Send me a photo of a business/name card and I'll take it from there! 📇")
        return

    note = update.message.text.strip()
    notes = f"{contact['notes']}\n{note}" if contact.get("notes") else note
    storage.update_contact(contact_id, notes=notes)
    await update.message.reply_text("✅ Note saved.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_application(token: str) -> Application:
    # Keeps pending cards and the in-progress notes conversation across
    # restarts. Reminders don't depend on it — they live in the database.
    persistence = PicklePersistence(
        filepath=os.environ.get("CARD_CONNECT_PERSISTENCE_PATH", "card_connect_persistence.pickle")
    )
    app = Application.builder().token(token).persistence(persistence).build()

    followup_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(save_start, pattern=r"^save:")],
        states={
            TOPIC: [
                CommandHandler("skip", skip_topic),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic),
            ],
            REQUEST: [
                CommandHandler("skip", skip_request),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_request),
            ],
            VOICE: [
                CommandHandler("skip", skip_voice),
                MessageHandler(filters.VOICE, receive_voice),
            ],
            VIP: [CallbackQueryHandler(receive_vip, pattern=r"^vip:(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_followup)],
        # Scanning a stack of cards: saving the next one restarts the questions
        # for that card instead of leaving its Save button dead.
        allow_reentry=True,
        name="followup_conv",
        persistent=True,
    )

    app.add_handler(TypeHandler(Update, gate), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("pipeline", pipeline_cmd))
    app.add_handler(CommandHandler("followups", followups_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("show", show_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(followup_conv)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_discard, pattern=r"^discard:"))
    app.add_handler(CallbackQueryHandler(contact_button, pattern=r"^(stage|snooze|delete|edit_notes):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Daily run, plus a catch-up shortly after start for anything that came
    # due while the bot was off.
    app.job_queue.run_daily(
        reminders_job,
        time=dtime(hour=REMINDER_HOUR, minute=0, tzinfo=TIMEZONE),
        name="daily_reminders",
    )
    app.job_queue.run_once(reminders_job, when=30, name="startup_reminders")
    return app


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — copy .env.example to .env and fill it in.")
    if not os.environ.get("LLM_GATEWAY_KEY", "").strip():
        raise SystemExit("LLM_GATEWAY_KEY is not set — see README > Configuration.")
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty — the bot will refuse everyone. Message it to learn your ID.")

    storage.init_db()
    app = build_application(token)

    logger.info(
        "Card Connect is online — LLM via %s, Google Contacts %s, reminders at %02d:00 %s",
        llm.GATEWAY_URL,
        "linked" if google_contacts.is_configured() else "not linked",
        REMINDER_HOUR,
        TIMEZONE.key,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
