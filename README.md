<div align="center">

# Card Connect

**A Telegram bot that turns a name-card photo into a saved Google Contact and a ready-to-send WhatsApp greeting.**

![Version](https://img.shields.io/badge/version-0.1.0-00D4C8)
![Python](https://img.shields.io/badge/-Python-3776AB?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/-Telegram-26A5E4?logo=telegram&logoColor=white)
![FreeLLMAPI](https://img.shields.io/badge/-FreeLLMAPI-D4A017)

</div>

---

## What it does

Send or forward a photo of a business/name card and Card Connect will:

1. Read the name, company, role/trade, phone, email, website, and address off it
2. Show you what it read and ask for confirmation — **nothing is saved or sent automatically**
3. On confirmation, save it as a Google Contact on your linked Google account (syncs to your phone via Google Contacts)
4. Hand back a `wa.me` click-to-chat link with a starter greeting pre-filled — you tap it to actually send

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

\* One of `GOOGLE_TOKEN_JSON` or a `token.json` file must be present.

## How extraction works

The photo is downscaled and sent to a vision model with a strict prompt asking for a single JSON object (`full_name`, `organization`, `title`, `phones`, `emails`, `website`, `address`) — fields are omitted rather than guessed if not printed on the card. Non-card images are rejected before anything is offered for saving.

## Project Structure

```
card-connect/
|-- main.py                # bot entry point, handlers, extraction prompt
|-- google_contacts.py     # People API contact creation
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

**Planned**

- Public web search of the contact (LinkedIn, company site, recent news) to surface likely pain points and conversation talking points
- Inline editing of extracted fields before saving, instead of accept/discard only
- Duplicate-contact detection (match against existing contacts by phone/email before creating a new one)
- Multi-card batch handling (album of several card photos in one message)
- Per-user rate limiting

## Changelog

- **v0.1.0** — *2026-08-18* — Initial release: name-card extraction, confirm-before-save flow, Google Contacts creation, WhatsApp click-to-chat link generation

---

<div align="center">
<sub>Built by <a href="https://github.com/TheBooleanJulian">@TheBooleanJulian</a></sub>
</div>
