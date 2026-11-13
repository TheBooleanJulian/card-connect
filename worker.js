// Card Connect on Cloudflare Workers.
//
//   POST /webhook   Telegram updates, checked against WEBHOOK_SECRET
//   cron 0 1 * * *  09:00 SGT: follow-up reminders, the day-3 / day-14 drafts, pruning
//   GET  /healthz   bot-core health JSON (the bot-core fan-out gates on it); / and /status branded page
//
// Send a photo of a name card: the model reads it through thebooleanjulian-llm (use case card-extract), you confirm,
// and the contact is saved to Supabase (schema card_connect, via Hyperdrive) and, when a Google account is linked,
// to Google Contacts. The bot then researches the person (card-intel), asks a few questions about the meeting,
// drafts the first follow-up (card-followup) and reminds you when each later one is due.
// Nothing is ever sent to a contact: every draft and the WhatsApp link are handed to you to send.
//
// A Worker keeps nothing in memory between updates, so cards awaiting Save/Discard and the "which question am I on"
// state live in Postgres too.

import { log, api, verifyWebhook, parseCommand, healthz, statusPage, serveBrand } from 'thebooleanjulian-bot-core';
import { withDb, dbCheck } from 'thebooleanjulian-bot-core/db';
import { createContact, isLinked } from './google.js';
import { DRAFTS, EXTRACT_SYSTEM, INTEL_SYSTEM } from './prompts.js';
import {
  ACTIVE_STAGES, FOLLOWUP_DAYS, LINKEDIN_NUDGE_DAYS, SECOND_TOUCH_DAYS, SECOND_TOUCH_STAGES, SNOOZE_DAYS, STAGES, STAGE_LABEL,
  contactContext, contactKeyboard, contactLines, esc, finaliseIntel, formatCardSummary, formatContact, formatIntel,
  formatPipeline, formatReminder, intelContext, normaliseCard, parseJson, parseSearchResults, searchQuery, whatsappLink
} from './core.js';

const SERVICE = 'card-connect';
const MAX_IMAGE_BYTES = 5 * 1024 * 1024; // the gateway's cap for card-extract
const MAX_CARDS_PER_BATCH = 10;          // follow-up cards per chat per run, to avoid flooding
const PENDING_TTL_DAYS = 7;

const HELP_TEXT =
  "Hi, I'm Card Connect! 📇\n\n" +
  "Send me a photo of a business/name card — or forward one from another chat — and I'll:\n" +
  '1️⃣ Read the name, company, role, phone and email off it\n' +
  '2️⃣ Ask you to confirm before saving anything\n' +
  '3️⃣ Save the contact and hand you a WhatsApp link with a greeting pre-filled — you tap it to actually send\n' +
  '4️⃣ Research them and suggest pain points and an opener\n' +
  '5️⃣ Draft your follow-ups and remind you when each is due\n\n' +
  '<b>Commands</b>\n' +
  '/pipeline — contacts by stage\n' +
  '/followups — follow-ups that are due now\n' +
  '/list — recent contacts\n' +
  '/search &lt;text&gt; — find by name, company, email, phone or notes\n' +
  "/show &lt;id&gt; — a contact's card and buttons\n" +
  '/delete &lt;id&gt; — delete a contact\n' +
  '/cancel — discard pending cards / stop the current questions\n\n' +
  'Nothing is ever sent to your contacts automatically.\n\n' +
  '⏰ Reminders arrive daily at 09:00 (Singapore time).';

const ASK = {
  topic: 'Quick log for smarter follow-ups — what did you two talk about? (or /skip)',
  request: 'What did they ask for / need from you? (or /skip)',
  voice: 'Optional: send a voice memo summarizing the vibe / key takeaway, or /skip.',
  vip: 'Is this a VIP / high-profile contact? Changes the follow-up path.'
};
const VIP_KEYBOARD = { inline_keyboard: [[{ text: 'Yes', callback_data: 'vip:yes' }, { text: 'No', callback_data: 'vip:no' }]] };
const NO_CONTACTS = '📭 No contacts yet — send me a name card.';

// ── model (thebooleanjulian-llm, over its service binding) ─────────────────
async function llm(env, useCase, messages, { maxTokens, json = false } = {}) {
  if (!env.LLM) throw new Error('LLM binding missing');
  const body = { messages };
  if (maxTokens) body.max_tokens = maxTokens;
  if (json) body.response_format = { type: 'json_object' };
  const res = await env.LLM.fetch('https://llm/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-TBJ-Service': SERVICE, 'X-TBJ-Use-Case': useCase },
    body: JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`gateway ${res.status} ${data?.error?.code || ''} trace ${res.headers.get('x-tbj-trace-id') || '-'}`);
  const text = String(data.choices?.[0]?.message?.content || '').trim();
  if (!text) throw new Error('gateway returned an empty reply');
  return text;
}

const draft = (env, kind, contact) =>
  llm(env, 'card-followup', [
    { role: 'system', content: DRAFTS[kind].system },
    { role: 'user', content: contactContext(contact, DRAFTS[kind].withRequest) }
  ], { maxTokens: DRAFTS[kind].maxTokens });

async function extractCard(env, dataUrl) {
  const reply = await llm(env, 'card-extract', [
    { role: 'system', content: EXTRACT_SYSTEM },
    { role: 'user', content: [{ type: 'image_url', image_url: { url: dataUrl } }, { type: 'text', text: 'Extract the contact details from this name card.' }] }
  ], { maxTokens: 1024 });
  return normaliseCard(parseJson(reply)); // null = not a name card
}

// One Google search via Serper.dev, only when SERPER_API_KEY is set. LinkedIn itself is never fetched.
async function searchPerson(env, contact) {
  const q = searchQuery(contact);
  if (!env.SERPER_API_KEY || !q) return { linkedinUrl: null, snippets: '' };
  try {
    const res = await fetch('https://google.serper.dev/search', {
      method: 'POST',
      headers: { 'X-API-KEY': env.SERPER_API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ q, num: 5 }),
      signal: AbortSignal.timeout(10000)
    });
    if (!res.ok) throw new Error(`status ${res.status}`);
    return parseSearchResults(await res.json());
  } catch (err) {
    log.warn('serper search failed', { error: err.message });
    return { linkedinUrl: null, snippets: '' };
  }
}

async function research(env, contact) {
  const found = await searchPerson(env, contact);
  const reply = await llm(env, 'card-intel', [
    { role: 'system', content: INTEL_SYSTEM },
    { role: 'user', content: intelContext(contact, found) }
  ], { maxTokens: 1800, json: true });
  return finaliseIntel(parseJson(reply), found);
}

// ── storage (schema card_connect) ──────────────────────────────────────────
const q = (env, fn) => withDb(env, null, fn);
const row = (r) => (r ? { ...r, id: Number(r.id) } : null); // bigint arrives as a string
const one = async (p) => row((await p)[0]);
const many = async (p) => (await p).map(row);
const jsonb = (v) => (v == null ? null : JSON.stringify(v));

const store = {
  save: (env, chatId, c, resourceName) => one(q(env, (sql) => sql`
    insert into card_connect.contacts
      (chat_id, full_name, organization, title, phones, emails, website, address, linkedin, google_resource_name,
       next_followup_at, linkedin_nudge_at, second_touch_at)
    values
      (${String(chatId)}, ${c.full_name ?? null}, ${c.organization ?? null}, ${c.title ?? null},
       ${jsonb(c.phones || [])}::jsonb, ${jsonb(c.emails || [])}::jsonb, ${c.website ?? null}, ${c.address ?? null},
       ${c.linkedin ?? null}, ${resourceName ?? null},
       now() + ${FOLLOWUP_DAYS.new}::int * interval '1 day',
       now() + ${LINKEDIN_NUDGE_DAYS}::int * interval '1 day',
       now() + ${SECOND_TOUCH_DAYS}::int * interval '1 day')
    returning *`)),
  get: (env, id, chatId) => one(q(env, (sql) => sql`select * from card_connect.contacts where id = ${id} and chat_id = ${String(chatId)}`)),
  // Sets the fields given; a field left out keeps its value.
  patch: (env, id, p) => q(env, (sql) => sql`
    update card_connect.contacts set
      topic = coalesce(${p.topic ?? null}::text, topic),
      request = coalesce(${p.request ?? null}::text, request),
      voice_file_id = coalesce(${p.voice_file_id ?? null}::text, voice_file_id),
      is_vip = coalesce(${p.is_vip ?? null}::boolean, is_vip),
      linkedin = coalesce(${p.linkedin ?? null}::text, linkedin),
      followup_opener = coalesce(${p.followup_opener ?? null}::text, followup_opener),
      intel = coalesce(${jsonb(p.intel)}::jsonb, intel)
    where id = ${id}`),
  addNote: (env, id, note) => q(env, (sql) => sql`
    update card_connect.contacts
    set notes = case when coalesce(notes, '') = '' then ${note}::text else notes || E'\n' || ${note}::text end
    where id = ${id}`),
  // days = null closes the pipeline for this contact (won / lost).
  setStage: (env, id, stage, days) => q(env, (sql) => sql`
    update card_connect.contacts
    set stage = ${stage}, stage_updated_at = now(), next_followup_at = now() + ${days}::int * interval '1 day'
    where id = ${id}`),
  snooze: (env, id) => q(env, (sql) => sql`
    update card_connect.contacts set next_followup_at = now() + ${SNOOZE_DAYS}::int * interval '1 day' where id = ${id}`),
  remove: async (env, id, chatId) => (await q(env, (sql) => sql`
    delete from card_connect.contacts where id = ${id} and chat_id = ${String(chatId)} returning id`)).length > 0,
  list: (env, chatId) => many(q(env, (sql) => sql`
    select * from card_connect.contacts where chat_id = ${String(chatId)} order by id desc limit 10`)),
  search: (env, chatId, text) => {
    const like = `%${text.replace(/[\\%_]/g, '\\$&')}%`;
    return many(q(env, (sql) => sql`
      select * from card_connect.contacts
      where chat_id = ${String(chatId)}
        and (full_name ilike ${like} or organization ilike ${like} or emails::text ilike ${like}
             or phones::text ilike ${like} or notes ilike ${like} or topic ilike ${like})
      order by id desc limit 20`));
  },
  pipeline: (env, chatId) => many(q(env, (sql) => sql`
    select * from card_connect.contacts where chat_id = ${String(chatId)} order by stage_updated_at desc limit 500`)),
  // Contacts in an open stage whose next follow-up has passed, oldest first. chatId null = every chat.
  dueFollowups: (env, chatId = null) => many(q(env, (sql) => sql`
    select * from card_connect.contacts
    where stage in ('new', 'contacted', 'meeting', 'proposal') and next_followup_at <= now()
      and (${chatId == null ? null : String(chatId)}::text is null or chat_id = ${chatId == null ? null : String(chatId)}::text)
    order by next_followup_at limit 200`)),
  dueLinkedin: (env) => many(q(env, (sql) => sql`select * from card_connect.contacts where linkedin_nudge_at <= now() order by linkedin_nudge_at limit 50`)),
  dueSecondTouch: (env) => many(q(env, (sql) => sql`select * from card_connect.contacts where second_touch_at <= now() order by second_touch_at limit 50`)),
  clearLinkedin: (env, id) => q(env, (sql) => sql`update card_connect.contacts set linkedin_nudge_at = null where id = ${id}`),
  clearSecondTouch: (env, id) => q(env, (sql) => sql`update card_connect.contacts set second_touch_at = null where id = ${id}`),

  putPending: (env, pid, chatId, card) => q(env, (sql) => sql`
    insert into card_connect.pending (pid, chat_id, card) values (${pid}, ${String(chatId)}, ${jsonb(card)}::jsonb)`),
  takePending: async (env, pid, chatId) => (await q(env, (sql) => sql`
    delete from card_connect.pending where pid = ${pid} and chat_id = ${String(chatId)} returning card`))[0]?.card ?? null,
  clearPending: (env, chatId) => q(env, (sql) => sql`delete from card_connect.pending where chat_id = ${String(chatId)}`),

  setConv: (env, chatId, contactId, step) => q(env, (sql) => sql`
    insert into card_connect.conversations (chat_id, contact_id, step) values (${String(chatId)}, ${contactId}, ${step})
    on conflict (chat_id) do update set contact_id = excluded.contact_id, step = excluded.step, updated_at = now()`),
  conv: async (env, chatId) => {
    const c = (await q(env, (sql) => sql`select contact_id, step from card_connect.conversations where chat_id = ${String(chatId)}`))[0];
    return c ? { contactId: Number(c.contact_id), step: c.step } : null;
  },
  endConv: (env, chatId) => q(env, (sql) => sql`delete from card_connect.conversations where chat_id = ${String(chatId)}`),

  prune: (env) => q(env, async (sql) => {
    await sql`delete from card_connect.pending where created_at < now() - ${PENDING_TTL_DAYS}::int * interval '1 day'`;
    await sql`delete from card_connect.conversations where updated_at < now() - interval '1 day'`;
  })
};

// ── telegram helpers ───────────────────────────────────────────────────────
const HTML = { parse_mode: 'HTML', link_preview_options: { is_disabled: true } };
const send = (env, chatId, text, extra = {}) => api(env, 'sendMessage', { chat_id: chatId, text, ...extra });
const edit = (env, cb, text, extra = {}) =>
  api(env, 'editMessageText', { chat_id: cb.message.chat.id, message_id: cb.message.message_id, text, ...extra });
const answer = (env, cb, text = '', alert = false) =>
  api(env, 'answerCallbackQuery', { callback_query_id: cb.id, ...(text ? { text, show_alert: alert } : {}) }).catch(() => {});

// Closed by default: the bot holds other people's contact details and spends model quota.
const allowed = (env, uid) =>
  String(env.ALLOWED_USER_IDS || '').split(/[,\s]+/).filter(Boolean).includes(String(uid));

const sendContact = (env, chatId, contact) =>
  send(env, chatId, formatContact(contact), { ...HTML, reply_markup: contactKeyboard(contact.id, contact.stage) });

async function fileDataUrl(env, fileId, mime = 'image/jpeg') {
  const file = await api(env, 'getFile', { file_id: fileId });
  const res = await fetch(`https://api.telegram.org/file/bot${env.BOT_TOKEN}/${file.file_path}`);
  if (!res.ok) throw new Error(`file download ${res.status}`);
  const bytes = await res.arrayBuffer();
  if (bytes.byteLength > MAX_IMAGE_BYTES) throw new Error('image too large');
  return `data:${mime};base64,${Buffer.from(bytes).toString('base64')}`;
}

// ── card photo -> confirm ──────────────────────────────────────────────────
async function onImage(env, msg) {
  const chatId = msg.chat.id;
  let fileId, mime = 'image/jpeg';
  if (msg.photo?.length) {
    fileId = msg.photo[msg.photo.length - 1].file_id; // largest size; Telegram has already compressed it
  } else {
    if ((msg.document.file_size || 0) > MAX_IMAGE_BYTES) {
      return send(env, chatId, "That file's too big for me (over 5 MB). Send it as a photo instead of a file and Telegram will shrink it.");
    }
    fileId = msg.document.file_id;
    mime = msg.document.mime_type;
  }

  const status = await send(env, chatId, 'Reading the card... 🔍');
  const done = (text) => api(env, 'editMessageText', { chat_id: chatId, message_id: status.message_id, text }).catch(() => {});

  let card;
  try {
    card = await extractCard(env, await fileDataUrl(env, fileId, mime));
  } catch (err) {
    log.error('extraction failed', { error: err.message });
    return done(err.message.startsWith('gateway')
      ? `I couldn't reach the model to read that card (${err.message}). Try again in a bit.`
      : "I couldn't make sense of what I read off that card — mind trying a clearer photo?");
  }
  if (!card) return done("I couldn't find a name on a business/name card in that image — send a clearer photo of one?");

  const pid = crypto.randomUUID().replace(/-/g, '').slice(0, 8);
  await store.putPending(env, pid, chatId, card);
  await api(env, 'deleteMessage', { chat_id: chatId, message_id: status.message_id }).catch(() => {});
  return send(env, chatId, `Here's what I read:\n\n${formatCardSummary(card)}\n\nSave this contact?`, {
    ...HTML,
    reply_markup: { inline_keyboard: [[{ text: '✅ Save contact', callback_data: `save:${pid}` }, { text: '❌ Discard', callback_data: `discard:${pid}` }]] }
  });
}

// ── save -> intel -> meeting notes -> first draft ──────────────────────────
async function onSave(env, cb, pid) {
  const chatId = cb.message.chat.id;
  const card = await store.takePending(env, pid, chatId);
  if (!card) return edit(env, cb, "This card's no longer pending — send a fresh photo to try again.");

  let resourceName = null;
  let where = "Saved to the bot's CRM (Google Contacts isn't linked).";
  if (isLinked(env)) {
    try {
      resourceName = await createContact(env, card);
      where = "Saved to Google Contacts and the bot's CRM.";
    } catch (err) {
      log.error('google contact failed', { error: err.message });
      where = "Saved to the bot's CRM. ⚠️ Google Contacts refused it — the linked account may need re-authorising.";
    }
  }

  const contact = await store.save(env, chatId, card, resourceName);
  const wa = (card.phones || []).map((p) => whatsappLink(p, card.full_name)).find(Boolean);
  await edit(env, cb,
    `✅ <b>${esc(card.full_name)}</b> — ${where}\n\n` +
    (wa ? `💬 <a href="${esc(wa)}">Tap to send a WhatsApp hi</a>` : '<i>No usable phone number found for a WhatsApp link.</i>'), HTML);
  await sendContact(env, chatId, contact);

  // Saving the next card of a stack restarts the questions for that card.
  await store.setConv(env, chatId, contact.id, 'topic');
  await send(env, chatId, ASK.topic);

  // Last, because it is the slow part: the questions above are already on screen while this runs.
  await sendIntel(env, chatId, contact);
}

// Research a saved contact, store the result and post it. Also behind the 🔍 Intel button, to retry or refresh.
async function sendIntel(env, chatId, contact) {
  const name = contact.full_name || 'this contact';
  let result;
  try {
    result = await research(env, contact);
  } catch (err) {
    log.error('intel failed', { contact: contact.id, error: err.message });
    return send(env, chatId, `⚠️ Couldn't research ${name} right now. Tap 🔍 Intel on their card to try again.`);
  }
  await store.patch(env, contact.id, {
    intel: result,
    followup_opener: result.follow_up_opener ? String(result.follow_up_opener) : null,
    linkedin: !contact.linkedin && result.linkedin_url ? result.linkedin_url : null
  });
  return send(env, chatId, formatIntel(result, name), HTML);
}

async function sendFirstDraft(env, chatId, contact) {
  const name = esc(contact.full_name || 'them');
  let text;
  try {
    if (contact.is_vip) {
      const [note, ask] = await Promise.all([draft(env, 'vip', contact), draft(env, 'secondMeeting', contact)]);
      text = `🌟 <b>VIP path for ${name}</b>\n\n` +
        'Skip email — send this as a handwritten note or a LinkedIn voice-note script (3-2-1 rule):\n\n' +
        `${esc(note)}\n\nWhen you're ready to ask for a second meeting, use something like:\n\n${esc(ask)}`;
    } else {
      text = `📧 <b>Follow-up draft for ${name}</b> (send within 24h):\n\n${esc(await draft(env, 'first', contact))}`;
    }
  } catch (err) {
    log.error('first draft failed', { contact: contact.id, error: err.message });
    text = "Saved your notes, but couldn't draft the follow-up message right now.";
  }
  return send(env, chatId, text, HTML);
}

// The questions after a save, one step per message: topic -> request -> voice -> vip (buttons).
// `value` is the answer to the current step, or null for /skip.
async function advance(env, chatId, conv, value) {
  if (conv.step === 'topic') {
    if (value) await store.patch(env, conv.contactId, { topic: value });
    await store.setConv(env, chatId, conv.contactId, 'request');
    return send(env, chatId, value ? `Got it. ${ASK.request}` : `No worries. ${ASK.request}`);
  }
  if (conv.step === 'request') {
    if (value) await store.patch(env, conv.contactId, { request: value });
    await store.setConv(env, chatId, conv.contactId, 'voice');
    return send(env, chatId, ASK.voice);
  }
  if (conv.step === 'voice') {
    if (value) await store.patch(env, conv.contactId, { voice_file_id: value });
    await store.setConv(env, chatId, conv.contactId, 'vip');
    return send(env, chatId, ASK.vip, { reply_markup: VIP_KEYBOARD });
  }
  return send(env, chatId, 'Tap Yes or No on the VIP question above, or /cancel.');
}

async function onVip(env, cb, choice) {
  const chatId = cb.message.chat.id;
  const conv = await store.conv(env, chatId);
  if (conv?.step !== 'vip') return edit(env, cb, 'Lost track of that follow-up — send a fresh card to try again.');
  const isVip = choice === 'yes';
  await store.patch(env, conv.contactId, { is_vip: isVip });
  await store.endConv(env, chatId);
  const contact = await store.get(env, conv.contactId, chatId);
  if (!contact) return edit(env, cb, 'That contact no longer exists.');
  await edit(env, cb, isVip ? 'VIP contact noted 🌟' : 'Got it — standard follow-up path.');
  return sendFirstDraft(env, chatId, contact);
}

// ── reminders ──────────────────────────────────────────────────────────────
// A header plus one actionable card per contact. reschedule (the daily run) pushes each contact's next follow-up
// out by SNOOZE_DAYS, so an ignored reminder comes back later instead of every day.
async function sendFollowupCards(env, chatId, contacts, reschedule) {
  const shown = contacts.slice(0, MAX_CARDS_PER_BATCH);
  await send(env, chatId, `⏰ <b>${contacts.length} follow-up${contacts.length === 1 ? '' : 's'} due</b>`, HTML);
  for (const c of shown) {
    try {
      await send(env, chatId, formatReminder(c), { ...HTML, reply_markup: contactKeyboard(c.id, c.stage) });
    } catch (err) {
      log.error('follow-up card failed', { contact: c.id, error: err.message });
      continue;
    }
    if (reschedule) await store.snooze(env, c.id);
  }
  if (contacts.length > shown.length) {
    await send(env, chatId, `…and ${contacts.length - shown.length} more. They'll come up in the next run, or use /followups.`);
  }
}

// One day-3 (LinkedIn comment) or day-14 (second-touch email) draft. The due date is cleared once the draft is
// sent, or straight away when the contact has moved past the point where it applies. If drafting or sending
// fails it stays set, so the next run retries.
async function sendNudge(env, contact, kind) {
  const name = esc(contact.full_name || 'them');
  const stage = contact.stage || 'new';
  if (kind === 'linkedin') {
    if (ACTIVE_STAGES.includes(stage)) {
      await send(env, contact.chat_id,
        `👀 <b>Day ${LINKEDIN_NUDGE_DAYS} nudge — ${name}</b>\n\n` +
        'Visit their LinkedIn, watch/read a recent post first, then comment something like:\n\n' +
        esc(await draft(env, 'linkedin', contact)), HTML);
    }
    return store.clearLinkedin(env, contact.id);
  }
  if (SECOND_TOUCH_STAGES.includes(stage)) {
    await send(env, contact.chat_id,
      `🔁 <b>Day ${SECOND_TOUCH_DAYS} check-in — ${name}</b>\n\n` +
      'No reply yet? Send this instead of "just circling back":\n\n' +
      esc(await draft(env, 'secondTouch', contact)), HTML);
  }
  return store.clearSecondTouch(env, contact.id);
}

// Everything that is due right now, for every chat. Driven entirely by due dates in the database,
// so a run that fails is simply picked up by the next one.
export async function runReminders(env) {
  const byChat = new Map();
  for (const c of await store.dueFollowups(env)) byChat.set(c.chat_id, [...(byChat.get(c.chat_id) || []), c]);
  for (const [chatId, contacts] of byChat) {
    await sendFollowupCards(env, chatId, contacts, true)
      .catch((err) => log.error('reminder batch failed', { error: err.message }));
  }

  let drafts = 0;
  for (const [kind, due] of [['linkedin', await store.dueLinkedin(env)], ['secondTouch', await store.dueSecondTouch(env)]]) {
    for (const contact of due) {
      try {
        await sendNudge(env, contact, kind);
        drafts += 1;
      } catch (err) {
        log.error('nudge failed, will retry', { kind, contact: contact.id, error: err.message });
      }
    }
  }
  log.info('reminder run', { chats: byChat.size, drafts });
}

// ── commands, buttons, text ────────────────────────────────────────────────
const idArg = (text) => (/^\d{1,15}$/.test(String(text || '').trim()) ? Number(String(text).trim()) : null);

async function onCommand(env, msg, cmd) {
  const chatId = msg.chat.id;
  switch (cmd.name) {
    case 'start':
    case 'help':
      return send(env, chatId, HELP_TEXT, HTML);
    case 'cancel':
      await store.clearPending(env, chatId);
      await store.endConv(env, chatId);
      return send(env, chatId, "Cleared. Send another card whenever you're ready.");
    case 'skip': {
      const conv = await store.conv(env, chatId);
      if (!conv || conv.step === 'note') return send(env, chatId, 'Nothing to skip right now.');
      return advance(env, chatId, conv, null);
    }
    case 'pipeline': {
      const contacts = await store.pipeline(env, chatId);
      return contacts.length ? send(env, chatId, formatPipeline(contacts), HTML) : send(env, chatId, NO_CONTACTS);
    }
    case 'followups': {
      const due = await store.dueFollowups(env, chatId);
      return due.length ? sendFollowupCards(env, chatId, due, false) : send(env, chatId, '🎉 No follow-ups due right now.');
    }
    case 'list': {
      const contacts = await store.list(env, chatId);
      if (!contacts.length) return send(env, chatId, NO_CONTACTS);
      return send(env, chatId, [`📋 <b>Recent contacts</b> (${contacts.length} shown) — /show &lt;id&gt; for details\n`, ...contactLines(contacts)].join('\n'), HTML);
    }
    case 'search': {
      if (!cmd.args) return send(env, chatId, 'Usage: /search <name, company, email, phone or note text>');
      const results = await store.search(env, chatId, cmd.args);
      if (!results.length) return send(env, chatId, `🔎 No results for “${cmd.args}”.`);
      return send(env, chatId, [`🔎 <b>Results for “${esc(cmd.args)}”</b> — /show &lt;id&gt; for details\n`, ...contactLines(results)].join('\n'), HTML);
    }
    case 'show': {
      const id = idArg(cmd.args);
      const contact = id == null ? null : await store.get(env, id, chatId);
      return contact ? sendContact(env, chatId, contact) : send(env, chatId, 'Usage: /show <id> — the id is the number shown by /list and /search.');
    }
    case 'delete': {
      const id = idArg(cmd.args);
      if (id == null) return send(env, chatId, 'Usage: /delete <id>');
      return send(env, chatId, (await store.remove(env, id, chatId))
        ? `🗑 Contact ${id} deleted from the bot's CRM. (A Google Contact, if one was saved, is untouched.)`
        : '❌ No contact with that id.');
    }
    default:
      return send(env, chatId, "I don't know that command — /help lists what I can do.");
  }
}

async function onMessage(env, msg) {
  const chatId = msg.chat.id;
  if (!msg.from) return;
  if (!allowed(env, msg.from.id)) {
    if (msg.chat.type !== 'private') return;
    return send(env, chatId, `⛔ Not authorised. Your Telegram user ID is ${msg.from.id} — if this is your bot, add it to ALLOWED_USER_IDS.`);
  }

  const cmd = msg.text?.startsWith('/') ? parseCommand(msg.text, env.BOT_USERNAME) : null;
  if (cmd) return onCommand(env, msg, cmd);
  if (msg.text?.startsWith('/')) return; // a command addressed to another bot

  if (msg.photo?.length || msg.document?.mime_type?.startsWith('image/')) return onImage(env, msg);

  const conv = await store.conv(env, chatId);
  if (msg.voice) {
    return conv?.step === 'voice' ? advance(env, chatId, conv, msg.voice.file_id) : send(env, chatId, 'Voice memos only attach right after a card is saved, when I ask for one.');
  }
  if (!msg.text) return;
  const text = msg.text.trim();
  if (conv?.step === 'note') {
    await store.addNote(env, conv.contactId, text);
    await store.endConv(env, chatId);
    return send(env, chatId, '✅ Note saved.');
  }
  if (conv?.step === 'topic' || conv?.step === 'request') return advance(env, chatId, conv, text);
  if (conv?.step === 'voice') return send(env, chatId, 'Send a voice memo, or /skip.');
  if (conv?.step === 'vip') return advance(env, chatId, conv, null);
  return send(env, chatId, "Send me a photo of a business/name card and I'll take it from there! 📇");
}

// save:<pid> | discard:<pid> | vip:yes|no | stage:<id>:<stage> | snooze:<id> | delete:<id> | note:<id> | intel:<id>
async function onCallback(env, cb) {
  if (!cb.message) return answer(env, cb);
  if (!allowed(env, cb.from?.id)) return answer(env, cb, '⛔ Not authorised.', true);
  const chatId = cb.message.chat.id;
  const [action, arg, stage] = String(cb.data || '').split(':');

  if (action === 'save') { await answer(env, cb); return onSave(env, cb, arg); }
  if (action === 'discard') {
    await answer(env, cb);
    return edit(env, cb, (await store.takePending(env, arg, chatId)) ? 'Discarded — nothing was saved.' : "This card's no longer pending — send a fresh photo to try again.");
  }
  if (action === 'vip') { await answer(env, cb); return onVip(env, cb, arg); }

  const id = idArg(arg);
  const contact = id == null ? null : await store.get(env, id, chatId);
  if (!contact) return answer(env, cb, 'That contact no longer exists.', true);

  if (action === 'stage') {
    if (!STAGES.includes(stage)) return answer(env, cb, 'Unknown stage.');
    const days = FOLLOWUP_DAYS[stage];
    await store.setStage(env, id, stage, days);
    await answer(env, cb, STAGE_LABEL[stage] + (days ? ` — next follow-up in ${days}d` : ' — pipeline closed'));
    // Fails with "message is not modified" when the same stage is tapped again: nothing to do.
    return api(env, 'editMessageReplyMarkup', { chat_id: chatId, message_id: cb.message.message_id, reply_markup: contactKeyboard(id, stage) }).catch(() => {});
  }
  if (action === 'snooze') {
    await store.snooze(env, id);
    return answer(env, cb, `😴 Snoozed ${SNOOZE_DAYS} days`);
  }
  if (action === 'delete') {
    await store.remove(env, id, chatId);
    await answer(env, cb);
    return edit(env, cb, "🗑 Contact deleted from the bot's CRM.");
  }
  if (action === 'note') {
    await store.setConv(env, chatId, id, 'note');
    await answer(env, cb);
    return send(env, chatId, `✏️ Send me the note to add for ${contact.full_name || 'this contact'}… (/cancel to stop)`);
  }
  if (action === 'intel') {
    await answer(env, cb, '🔍 Researching…');
    return sendIntel(env, chatId, contact);
  }
  return answer(env, cb);
}

async function handleUpdate(env, update) {
  try {
    if (update.message) await onMessage(env, update.message);
    else if (update.callback_query) await onCallback(env, update.callback_query);
  } catch (err) {
    // Never let one bad update fail the webhook: Telegram would retry it forever.
    log.error('update failed', { update_id: update.update_id, error: err.message });
  }
}

// ── http ───────────────────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);

    const brand = serveBrand(request);
    if (brand) return brand;
    if (pathname === '/healthz') {
      return healthz(env, { checks: { postgres: await dbCheck(env, ctx), gateway: env.LLM ? 'ok' : 'FAIL: LLM binding missing' } });
    }
    if (pathname === '/' || pathname === '/status') {
      return statusPage(env, {
        name: 'Card Connect',
        username: env.BOT_USERNAME ? `@${env.BOT_USERNAME}` : '',
        icon: '📇',
        description: 'Send a photo of a name card: it is read, saved on your say-so, researched, and followed up through a sales pipeline. Reminders at 09:00 SGT.',
        ok: (await dbCheck(env, ctx)) === 'ok',
        commands: [['/pipeline', 'Contacts by stage'], ['/followups', 'Follow-ups due now'], ['/list', 'Recent contacts'],
          ['/search <text>', 'Find a contact'], ['/show <id>', "A contact's card"], ['/delete <id>', 'Delete a contact']]
      });
    }
    if (pathname === '/webhook' && request.method === 'POST') {
      if (!verifyWebhook(request, env)) return new Response('forbidden', { status: 403 });
      const update = await request.json().catch(() => null);
      if (update) ctx.waitUntil(handleUpdate(env, update));
      return new Response('ok');
    }
    return new Response('not found', { status: 404 });
  },

  async scheduled(event, env) {
    await runReminders(env).catch((err) => log.error('reminders failed', { error: err.message }));
    await store.prune(env).catch((err) => log.error('prune failed', { error: err.message }));
  }
};
