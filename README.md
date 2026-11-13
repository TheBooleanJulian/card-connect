<div align="center">

# Card Connect

**A Telegram bot that turns a name-card photo into a saved contact, a ready-to-send WhatsApp greeting, background intel and a follow-up pipeline.**

![Version](https://img.shields.io/badge/version-0.3.0-00D4C8)
![Python](https://img.shields.io/badge/-Python-3776AB?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/-Telegram-26A5E4?logo=telegram&logoColor=white)
![License](https://img.shields.io/badge/license-AGPLv3%20%2B%20Commercial-00D4C8.svg)

</div>

---

## What it does

Send or forward a photo of a business/name card and Card Connect will:

1. Read the name, company, role/trade, phone, email, website, address and LinkedIn off it
2. Show you what it read and ask for confirmation — **nothing is saved or sent automatically**
3. On confirmation, save it to its own CRM (SQLite) and, if you've linked a Google account, as a Google Contact (syncs to your phone)
4. Hand back a `wa.me` click-to-chat link with a starter greeting pre-filled — you tap it to actually send
5. Research the person in the background: current positions, three likely pain points with how you could help, and a suggested opener
6. Walk you through a post-event follow-up cadence and track the contact through a sales pipeline, reminding you when each follow-up is due — all as copy-and-send text, never sent automatically

### The follow-up cadence

After you confirm a save, Card Connect asks a few quick questions (`/skip` any of them):

1. **What did you talk about?** and **what did they ask for?** — logged against the contact so later drafts aren't generic
2. **Optional voice memo** — attach a quick "vibe" note, saved for your own reference
3. **VIP contact?** — high-profile contacts get a handwritten-note / LinkedIn-voice-note script (3-2-1 rule: 3 sentences, 2 specific references, 1 ask) instead of an email draft

It then immediately drafts your first follow-up (the "3 C's": Context, Compliment, low-friction Call-to-Action), and schedules two one-off drafts:

- **Day 3** — a specific, non-generic LinkedIn comment to leave after engaging with their post
- **Day 14** — a low-pressure "quick update" email to use instead of a "just circling back" nudge. Skipped if the contact has already moved past *Contacted* in the pipeline

### The pipeline

Every saved contact starts at **New** and moves through **Contacted → Meeting → Proposal → Won / Lost** with the buttons under its card. Entering a stage schedules the next follow-up (2, 3, 2 and 4 days respectively); Won and Lost close it. When a follow-up comes due you get a card with the contact's details, their top pain point and the suggested opener, plus buttons to move the stage, add a note, snooze 3 days or delete. An ignored reminder comes back after 3 days rather than every day.

Reminders go out once a day (09:00 Singapore time by default) and once shortly after the bot starts, to catch anything that came due while it was off. All due dates live in the database, so restarts never lose one.

| Command | |
|---|---|
| `/pipeline` | Contacts by stage, overdue ones flagged |
| `/followups` | Follow-ups that are due now |
| `/list` | Recent contacts |
| `/search <text>` | Find by name, company, email, phone or notes |
| `/show <id>` | A contact's full card and buttons |
| `/delete <id>` | Delete a contact from the bot's CRM (a Google Contact is left alone) |
| `/cancel` | Discard pending cards / stop the current questions |

### Why a link instead of auto-sending WhatsApp

WhatsApp's Business API only allows automated freeform messages to numbers that messaged *you* first within the last 24 hours — a cold "hi" to someone you just met requires a pre-approved message template. Unofficial automation (driving a personal WhatsApp Web session) violates WhatsApp's Terms of Service and risks account bans. A `wa.me` link sidesteps both problems: it opens the chat with the message pre-filled, and you send it with one tap.

## Tech Stack

| Layer | Choice |
|---|---|
| Bot | python-telegram-bot 21.6 (polling) |
| Models | The [thebooleanjulian-llm](https://github.com/TheBooleanJulian/thebooleanjulian-llm) gateway (`https://llm.thebooleanjulian.dev`). The bot holds one service key and names a use case (`card-extract`, `card-followup`, `card-intel`); the gateway picks the provider and model and holds the provider keys. Contact data is someone else's personal data, so all three use cases are marked sensitive and never go to free-tier pools |
| Storage | SQLite (`card_connect.db`) |
| Contacts | Google People API (OAuth, single linked account) — optional |
| Research | One Google search per contact via [Serper.dev](https://serper.dev) — optional. LinkedIn itself is never fetched |
| WhatsApp | `wa.me` click-to-chat links (no API, no automation) |
| Image processing | Pillow |

## Quick Start

```bash
cd card-connect
pip install -r requirements.txt
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, LLM_GATEWAY_KEY and ALLOWED_USER_IDS — see Configuration below
python main.py
```

The bot polls Telegram for updates — no public URL or webhook needed.

**Your user ID:** with `ALLOWED_USER_IDS` empty the bot refuses everyone, and its refusal tells you your Telegram user ID. Put that in `.env` and restart.

**The gateway key:** in the `thebooleanjulian-llm` repo run `node scripts/new-service-key.mjs card-connect`. The key lands on the clipboard (that's `LLM_GATEWAY_KEY`); add the printed hash to the gateway's `SERVICE_KEYS` variable. See that repo's `docs/onboarding-a-service.md`.

### Google Contacts setup (optional, one-time)

Without this, contacts are kept in the bot's own CRM only.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project (or reuse one) and enable the **People API**.
2. Configure the OAuth consent screen (External is fine; add your own Gmail as a test user if it stays in Testing mode).
3. Create an **OAuth client ID** of type **Desktop app**, download it, and save it as `credentials.json` in this folder.
4. Run the linking script — it opens a browser for you to sign in and approve access:
   ```bash
   python authorize_google.py
   ```
5. This writes `token.json`. Keep it next to `main.py` for local runs, or copy its contents into `GOOGLE_TOKEN_JSON` for deployment.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token from [@BotFather](https://t.me/BotFather) |
| `LLM_GATEWAY_KEY` | ✅ | `card-connect` service key for the thebooleanjulian-llm gateway |
| `ALLOWED_USER_IDS` | ✅ | Comma-separated Telegram user IDs allowed to use the bot. Empty means nobody |
| `LLM_GATEWAY_URL` | ❌ | Defaults to `https://llm.thebooleanjulian.dev` |
| `SERPER_API_KEY` | ❌ | Serper.dev key for contact research. Without it, intel is inferred from the card alone and labelled low-confidence |
| `GOOGLE_TOKEN_JSON` | ❌ | Contents of `token.json`, inline — for hosts where a persistent file is inconvenient |
| `GOOGLE_TOKEN_PATH` | ❌ | Path to `token.json` if not using `GOOGLE_TOKEN_JSON`. Defaults to `token.json` |
| `TIMEZONE` | ❌ | Timezone for the daily reminder run. Defaults to `Asia/Singapore` |
| `REMINDER_HOUR` | ❌ | Hour of the daily reminder run. Defaults to `9` |
| `CARD_CONNECT_DB_PATH` | ❌ | SQLite file for contacts, notes, pipeline state and due dates. Defaults to `card_connect.db` |
| `CARD_CONNECT_PERSISTENCE_PATH` | ❌ | Pickle file for cards awaiting confirmation and the in-progress questions. Defaults to `card_connect_persistence.pickle` |

## How extraction works

The photo is downscaled and sent to a vision model with a strict prompt asking for a single JSON object (`full_name`, `organization`, `title`, `phones`, `emails`, `website`, `address`, `linkedin`) — fields are omitted rather than guessed if not printed on the card. Non-card images are rejected before anything is offered for saving.

## How the intel works

With a Serper key, the bot runs one Google search for the person and passes the result snippets, the card and your meeting notes to the model. Positions and the LinkedIn link come only from the card or those results; the pain points are informed guesses and are labelled as likely, with a confidence level. Without a key the model has the card alone, and the result is marked low-confidence.

## Tests

```bash
python -m unittest discover -s tests
```

No network needed: the gateway and Telegram are faked.

## Project Structure

```
card-connect/
|-- main.py                # bot entry point: handlers, notes conversation, reminders, CRM commands
|-- extraction.py          # name-card photo -> contact details (prompt, image prep, validation)
|-- intel.py               # contact research: Serper search + synthesis
|-- followup.py            # drafting for each follow-up phase (email, LinkedIn comment, VIP note)
|-- pipeline.py            # stages, follow-up schedule, message formatting
|-- storage.py             # SQLite store
|-- llm.py                 # thebooleanjulian-llm gateway client
|-- google_contacts.py     # People API contact creation
|-- authorize_google.py    # one-time OAuth linking script
|-- tests/
|-- requirements.txt
|-- .env.example
`-- README.md
```

## Status / Roadmap

**Done**

- [x] Photo, clipboard paste, and forwarded image handling
- [x] Structured JSON extraction (name, org, title, phone, email, website, address, LinkedIn)
- [x] Confirm-before-save flow with inline keyboard
- [x] Google Contacts creation via People API
- [x] `wa.me` click-to-chat link generation with a pre-filled greeting
- [x] Post-event follow-up cadence: meeting notes, drafted first-touch email/VIP note, day-3 LinkedIn nudge, day-14 second-touch draft
- [x] Contact research: positions, likely pain points, suggested opener
- [x] Sales pipeline with stage-based follow-up reminders, snooze, notes, search
- [x] Allow-list of Telegram users

**Planned**

- Inline editing of extracted fields before saving, instead of accept/discard only
- Duplicate-contact detection (match against existing contacts by phone/email before creating a new one)
- Multi-card batch handling (album of several card photos in one message)

## Changelog

- **v0.3.0** — *2026-11-14* — Merged in the separate "Namecard CRM Bot" build (card-ocr): sales pipeline with stage buttons, daily follow-up reminders, snooze, notes, `/pipeline` `/followups` `/list` `/search` `/show` `/delete`, contact research, user allow-list. All model calls now go through the thebooleanjulian-llm gateway (FreeLLMAPI and the OpenAI SDK are gone). Day-3 / day-14 drafts are scheduled in the database instead of in-memory jobs, which did not survive a restart. Google Contacts is now optional
- **v0.2.0** — *2026-08-23* — Post-event follow-up cadence: meeting notes capture (topic/request/voice memo), VIP path with 3-2-1 handwritten-note drafts, LLM-drafted first-touch/LinkedIn-comment/second-touch messages, day-3 and day-14 reminders
- **v0.1.0** — *2026-08-18* — Initial release: name-card extraction, confirm-before-save flow, Google Contacts creation, WhatsApp click-to-chat link generation

## License

This project is dual licensed.

- Community Edition — [GNU Affero General Public License v3 (AGPLv3)](LICENSE). Free to use, modify, and self-host. If you distribute a modified version or run it as a network service, you must make the corresponding source available.
- Commercial License — for organisations that want to embed, modify, or distribute this software without AGPLv3's obligations. See [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

---

<div align="center">
<sub>Built by <a href="https://github.com/TheBooleanJulian">@TheBooleanJulian</a></sub>
</div>
