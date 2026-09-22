<div align="center">

# Card Connect

**A Telegram bot that turns a name-card photo into a saved Google Contact and a ready-to-send WhatsApp greeting.**

![Version](https://img.shields.io/badge/version-0.1.0-00D4C8)
![Python](https://img.shields.io/badge/-Python-3776AB?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/-Telegram-26A5E4?logo=telegram&logoColor=white)
![FreeLLMAPI](https://img.shields.io/badge/-FreeLLMAPI-D4A017)
![License](https://img.shields.io/badge/license-AGPLv3%20%2B%20Commercial-00D4C8.svg)

</div>

---

## What it does

Send or forward a photo of a business/name card and Card Connect will:

1. Read the name, company, role/trade, phone, email, website, and address off it
2. Show you what it read and ask for confirmation — **nothing is saved or sent automatically**
3. On confirmation, save it as a Google Contact on your linked Google account (syncs to your phone via Google Contacts)
4. Hand back a `wa.me` click-to-chat link with a starter greeting pre-filled — you tap it to actually send
5. Walk you through a post-event follow-up cadence: log what you discussed and what they asked for, get a drafted first-touch email (or a handwritten-note script for VIP contacts) right away, then a day-3 LinkedIn-engagement nudge and a day-14 second-touch draft — all as copy-and-send text, never sent automatically

### The follow-up cadence

After you confirm a save, Card Connect asks a few quick questions (`/skip` any of them):

1. **What did you talk about?** and **what did they ask for?** — logged against the contact so later drafts aren't generic
2. **Optional voice memo** — attach a quick "vibe" note, saved for your own reference
3. **VIP contact?** — high-profile contacts get a handwritten-note / LinkedIn-voice-note script (3-2-1 rule: 3 sentences, 2 specific references, 1 ask) instead of an email draft

It then immediately drafts your first follow-up (the "3 C's": Context, Compliment, low-friction Call-to-Action), and schedules two reminders sent back to you on Telegram:

- **Day 3** — a specific, non-generic LinkedIn comment to leave after engaging with their post
- **Day 14** — a low-pressure "quick update" email draft to use instead of a "just circling back" nudge, in case they haven't replied

Reminders are persisted (`card_connect_persistence.pickle`) so a restart doesn't drop them.

Card Connect runs on the same self-hosted [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) setup as Miku OCR — an OpenAI-compatible proxy that routes requests across free-tier vision models.

### Why a link instead of auto-sending WhatsApp

WhatsApp's Business API only allows automated freeform messages to numbers that messaged *you* first within the last 24 hours — a cold "hi" to someone you just met requires a pre-approved message template. Unofficial automation (driving a personal WhatsApp Web session) violates WhatsApp's Terms of Service and risks account bans. A `wa.me` link sidesteps both problems: it opens the chat with the message pre-filled, and you send it with one tap.

## Tech Stack

| Layer | Choice |
|---|---|
| Bot | python-telegram-bot 21.6 (polling) |
| Extraction | Free-tier vision models via a self-hosted [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) proxy (OpenAI-compatible), prompted for structured JSON |
| Contacts | Google People API (OAuth, single linked account) |
| WhatsApp | `wa.me` click-to-chat links (no API, no automation) |
| Image processing | Pillow |

## Quick Start

```bash
cd card-connect
pip install -r requirements.txt
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN and FREELLMAPI_API_KEY — see Configuration below
```

### Google Contacts setup (one-time)

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project (or reuse one) and enable the **People API**.
2. Configure the OAuth consent screen (External is fine; add your own Gmail as a test user if it stays in Testing mode).
3. Create an **OAuth client ID** of type **Desktop app**, download it, and save it as `credentials.json` in this folder.
4. Run the linking script — it opens a browser for you to sign in and approve access:
   ```bash
   python authorize_google.py
   ```
5. This writes `token.json`. Keep it next to `main.py` for local runs, or copy its contents into `GOOGLE_TOKEN_JSON` for deployment.

### Run

```bash
python main.py
```

The bot polls Telegram for updates — no public URL or webhook needed. You'll also need a running FreeLLMAPI instance with at least one vision-capable provider key configured.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token from [@BotFather](https://t.me/BotFather) |
| `FREELLMAPI_API_KEY` | ✅ | Unified key from your self-hosted FreeLLMAPI dashboard |
| `FREELLMAPI_BASE_URL` | ❌ | FreeLLMAPI's `/v1` endpoint. Defaults to `http://localhost:3001/v1` |
| `FREELLMAPI_MODEL` | ❌ | Model id to pin, or `auto` (default) to use FreeLLMAPI's fallback chain |
| `GOOGLE_TOKEN_JSON` | ❌* | Contents of `token.json`, inline — for hosts where a persistent file is inconvenient |
| `GOOGLE_TOKEN_PATH` | ❌ | Path to `token.json` if not using `GOOGLE_TOKEN_JSON`. Defaults to `token.json` |
| `CARD_CONNECT_DB_PATH` | ❌ | SQLite file for saved contacts' follow-up notes. Defaults to `card_connect.db` |
| `CARD_CONNECT_PERSISTENCE_PATH` | ❌ | Pickle file for the bot's conversation state and scheduled reminder jobs. Defaults to `card_connect_persistence.pickle` |

\* One of `GOOGLE_TOKEN_JSON` or a `token.json` file must be present.

## How extraction works

The photo is downscaled and sent to a vision model with a strict prompt asking for a single JSON object (`full_name`, `organization`, `title`, `phones`, `emails`, `website`, `address`) — fields are omitted rather than guessed if not printed on the card. Non-card images are rejected before anything is offered for saving.

## Screenshots

_Screenshots coming soon._

## Project Structure

```
card-connect/
|-- main.py                # bot entry point, handlers, extraction prompt, follow-up conversation
|-- google_contacts.py     # People API contact creation
|-- storage.py             # SQLite store for follow-up notes (topic, request, VIP flag, voice memo)
|-- followup.py            # LLM drafting for each follow-up phase (email, LinkedIn comment, etc.)
|-- llm.py                 # shared FreeLLMAPI client
|-- authorize_google.py    # one-time OAuth linking script
|-- requirements.txt
|-- .env.example
`-- README.md
```

## Status / Roadmap

**Done**

- [x] Photo, clipboard paste, and forwarded image handling
- [x] Structured JSON extraction (name, org, title, phone, email, website, address)
- [x] Confirm-before-save flow with inline keyboard
- [x] Google Contacts creation via People API
- [x] `wa.me` click-to-chat link generation with a pre-filled greeting
- [x] Post-event follow-up cadence: meeting notes, drafted first-touch email/VIP note, day-3 LinkedIn nudge, day-14 second-touch draft

**Planned**

- Public web search of the contact (LinkedIn, company site, recent news) to surface likely pain points and conversation talking points
- Inline editing of extracted fields before saving, instead of accept/discard only
- Duplicate-contact detection (match against existing contacts by phone/email before creating a new one)
- Multi-card batch handling (album of several card photos in one message)
- Per-user rate limiting

## Changelog

- **v0.2.0** — *2026-08-23* — Post-event follow-up cadence: meeting notes capture (topic/request/voice memo), VIP path with 3-2-1 handwritten-note drafts, LLM-drafted first-touch/LinkedIn-comment/second-touch messages, day-3 and day-14 reminders persisted across restarts
- **v0.1.0** — *2026-08-18* — Initial release: name-card extraction, confirm-before-save flow, Google Contacts creation, WhatsApp click-to-chat link generation

## License

This project is dual licensed.

- Community Edition — [GNU Affero General Public License v3 (AGPLv3)](LICENSE). Free to use, modify, and self-host. If you distribute a modified version or run it as a network service, you must make the corresponding source available.
- Commercial License — for organisations that want to embed, modify, or distribute this software without AGPLv3's obligations. See [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

---

<div align="center">
<sub>Built by <a href="https://github.com/TheBooleanJulian">@TheBooleanJulian</a></sub>
</div>
