// node --test   The whole webhook flow of worker.js against a real (embedded) Postgres: PGlite runs schema.sql, and a small
// adapter gives it the postgres.js tagged-template shape (env.SQL, as bot-core/db allows). The gateway binding is a fake,
// and global fetch is stubbed for Telegram, Serper and Google.
import { test, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';
import worker, { runReminders } from '../worker.js';

let pg;
before(async () => {
  pg = new PGlite();
  await pg.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
});
beforeEach(() => pg.exec('truncate card_connect.contacts, card_connect.pending, card_connect.conversations restart identity cascade'));

const sql = async (strings, ...values) => {
  const text = strings.reduce((acc, s, i) => acc + s + (i < values.length ? `$${i + 1}` : ''), '');
  return (await pg.query(text, values)).rows;
};
sql.end = async () => {};
const rows = async (text) => (await pg.query(text)).rows;

const CARD = { full_name: 'Tan Wei Ming', organization: 'Acme <Pte> Ltd', title: 'CTO', phones: ['+65 9123 4567'], emails: ['tan@acme.sg'] };
const INTEL = {
  linkedin_url: 'https://linkedin.com/in/invented', current_positions: [{ title: 'CTO', company: 'Acme' }],
  pain_points: [{ pain: 'Manual ops', detail: 'Likely slow.', opportunity: 'Automation' }, { pain: 'B' }, { pain: 'C' }],
  follow_up_opener: 'Great meeting you!', confidence: 'high', data_source: 'search_snippets'
};

function setup(overrides = {}) {
  const calls = [];
  const llm = [];
  const google = [];
  const state = { extract: JSON.stringify(CARD), failIntel: false, serper: null };
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.includes('api.telegram.org/file/')) return new Response(new Uint8Array([0xff, 0xd8, 0xff]));
    if (u.includes('api.telegram.org')) {
      const method = u.split('/').pop();
      calls.push({ method, body: JSON.parse(init.body || '{}') });
      if (method === 'getFile') return Response.json({ ok: true, result: { file_path: 'photos/x.jpg' } });
      return Response.json({ ok: true, result: { message_id: 100 + calls.length } });
    }
    if (u.includes('google.serper.dev')) return Response.json(state.serper || {});
    if (u.includes('oauth2.googleapis.com')) return Response.json({ access_token: 'tok' });
    if (u.includes('people.googleapis.com')) { google.push(JSON.parse(init.body)); return Response.json({ resourceName: 'people/c1' }); }
    throw new Error(`unexpected fetch ${u}`);
  };
  const env = {
    SQL: sql, BOT_TOKEN: '123:abc', WEBHOOK_SECRET: 's3', ALLOWED_USER_IDS: '42, 43', BOT_USERNAME: 'cardconnectbot',
    LLM: {
      fetch: async (_u, init) => {
        const useCase = init.headers['X-TBJ-Use-Case'];
        llm.push({ useCase, service: init.headers['X-TBJ-Service'], body: JSON.parse(init.body) });
        if (useCase === 'card-intel' && state.failIntel) return Response.json({ error: { code: 'all_providers_failed' } }, { status: 502 });
        const content = useCase === 'card-extract' ? state.extract : useCase === 'card-intel' ? JSON.stringify(INTEL) : `draft #${llm.length}`;
        return Response.json({ choices: [{ message: { content } }] });
      }
    },
    ...overrides
  };
  return { env, calls, llm, google, state };
}

const post = async (env, update, secret = 's3') => {
  const tasks = [];
  const res = await worker.fetch(new Request('https://x/webhook', { method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': secret }, body: JSON.stringify(update) }), env, { waitUntil: (p) => tasks.push(p) });
  await Promise.all(tasks);
  return res;
};
const msg = (fields, from = 42) => ({ update_id: 1, message: { message_id: 10, from: { id: from }, chat: { id: 42, type: 'private' }, ...fields } });
const text = (t, from) => msg({ text: t }, from);
const photo = () => msg({ photo: [{ file_id: 'small' }, { file_id: 'big' }] });
const tap = (data, from = 42) => ({ update_id: 2, callback_query: { id: 'q', from: { id: from }, data, message: { message_id: 7, chat: { id: 42, type: 'private' } } } });
const sent = (calls) => calls.filter((c) => c.method === 'sendMessage' || c.method === 'editMessageText').map((c) => c.body.text);
const lastText = (calls) => sent(calls).at(-1);

// Photo -> Save. Returns the saved contact's id.
async function saveCard(env) {
  await post(env, photo());
  const [{ pid }] = await rows('select pid from card_connect.pending');
  await post(env, tap(`save:${pid}`));
  return Number((await rows('select id from card_connect.contacts order by id desc limit 1'))[0].id);
}

test('rejects webhook calls without the secret', async () => {
  const { env, calls } = setup();
  assert.equal((await post(env, text('/list'), 'wrong')).status, 403);
  assert.equal(calls.length, 0);
});

test('allow-list is closed by default and tells a stranger their id, nothing else', async () => {
  const { env, calls, llm } = setup({ ALLOWED_USER_IDS: '' });
  await post(env, photo());
  assert.match(lastText(calls), /Not authorised\. Your Telegram user ID is 42/);
  assert.equal(llm.length, 0);

  const second = setup();
  await post(second.env, text('/list', 99));
  assert.match(lastText(second.calls), /user ID is 99/);
  await post(second.env, tap('delete:1', 99));
  assert.equal(second.calls.at(-1).body.show_alert, true);
});

test('photo -> confirm card, read through the card-extract use case, nothing saved yet', async () => {
  const { env, calls, llm } = setup();
  await post(env, photo());
  assert.equal(llm[0].useCase, 'card-extract');
  assert.equal(llm[0].service, 'card-connect');
  assert.match(llm[0].body.messages[1].content[0].image_url.url, /^data:image\/jpeg;base64,/);
  assert.equal(calls.find((c) => c.method === 'getFile').body.file_id, 'big');
  const confirm = calls.at(-1).body;
  assert.match(confirm.text, /Tan Wei Ming/);
  assert.match(confirm.text, /Acme &lt;Pte&gt; Ltd/);
  assert.equal(confirm.reply_markup.inline_keyboard[0][0].text, '✅ Save contact');
  assert.equal((await rows('select 1 from card_connect.contacts')).length, 0);
});

test('a non-card image and an oversized file are turned away', async () => {
  const { env, calls, state, llm } = setup();
  state.extract = '{"error": "NOT_A_NAMECARD"}';
  await post(env, photo());
  assert.match(lastText(calls), /couldn't find a name/);
  assert.equal((await rows('select 1 from card_connect.pending')).length, 0);

  await post(env, msg({ document: { file_id: 'd', mime_type: 'image/png', file_size: 9 * 1024 * 1024 } }));
  assert.match(lastText(calls), /too big/);
  assert.equal(llm.length, 1);
});

test('discard saves nothing; a stale button says so', async () => {
  const { env, calls } = setup();
  await post(env, photo());
  const [{ pid }] = await rows('select pid from card_connect.pending');
  await post(env, tap(`discard:${pid}`));
  assert.match(lastText(calls), /Discarded/);
  await post(env, tap(`save:${pid}`));
  assert.match(lastText(calls), /no longer pending/);
  assert.equal((await rows('select 1 from card_connect.contacts')).length, 0);
});

test('save -> WhatsApp link, contact card, intel, then the notes questions and a first draft', async () => {
  const { env, calls, llm, google } = setup();
  const id = await saveCard(env);

  const texts = sent(calls);
  assert.ok(texts.some((t) => /Saved to the bot's CRM \(Google Contacts isn't linked\)/.test(t) && t.includes('https://wa.me/6591234567?text=')));
  assert.equal(google.length, 0);
  assert.ok(texts.some((t) => t.includes('what did you two talk about')));
  // No search key: the model's "high confidence" and its invented LinkedIn URL are both overruled.
  const intelMsg = texts.find((t) => t.startsWith('🔍 <b>Intel'));
  assert.match(intelMsg, /Confidence: <b>low<\/b> — inferred from the card alone/);
  assert.ok(!intelMsg.includes('linkedin.com'));

  let [c] = await rows('select * from card_connect.contacts');
  assert.equal(c.stage, 'new');
  assert.equal(c.followup_opener, 'Great meeting you!');
  assert.equal(c.linkedin, null);
  assert.deepEqual(c.phones, ['+65 9123 4567']);
  assert.ok(c.next_followup_at > new Date() && c.linkedin_nudge_at > c.next_followup_at && c.second_touch_at > c.linkedin_nudge_at);

  await post(env, text('AI bots for their sales team'));
  await post(env, text('/skip'));
  assert.match(lastText(calls), /voice memo/);
  await post(env, text('typed instead of spoken'));
  assert.match(lastText(calls), /Send a voice memo, or \/skip/);
  await post(env, msg({ voice: { file_id: 'voice1' } }));
  assert.match(lastText(calls), /VIP/);
  await post(env, tap('vip:no'));

  [c] = await rows('select * from card_connect.contacts');
  assert.deepEqual([c.topic, c.request, c.voice_file_id, c.is_vip], ['AI bots for their sales team', null, 'voice1', false]);
  assert.equal((await rows('select 1 from card_connect.conversations')).length, 0);
  assert.match(lastText(calls), /Follow-up draft for Tan Wei Ming/);
  const draftCall = llm.at(-1);
  assert.equal(draftCall.useCase, 'card-followup');
  assert.match(draftCall.body.messages[1].content, /Topic discussed: AI bots for their sales team/);
  assert.equal(id, Number(c.id));
});

test('with a Google account linked and a search key: contact goes to Google, LinkedIn comes from the search', async () => {
  const { env, calls, google, state, llm } = setup({ GOOGLE_TOKEN_JSON: JSON.stringify({ client_id: 'c', client_secret: 's', refresh_token: 'r' }), SERPER_API_KEY: 'k' });
  state.serper = { organic: [{ link: 'https://sg.linkedin.com/in/tanweiming?x=1', title: 'Tan Wei Ming - CTO - Acme', snippet: 'CTO at Acme since 2020' }] };
  await saveCard(env);
  assert.equal(google[0].names[0].familyName, 'Ming');
  const [c] = await rows('select * from card_connect.contacts');
  assert.equal(c.google_resource_name, 'people/c1');
  assert.equal(c.linkedin, 'https://sg.linkedin.com/in/tanweiming');
  assert.match(llm.find((l) => l.useCase === 'card-intel').body.messages[1].content, /CTO at Acme since 2020/);
  assert.ok(sent(calls).some((t) => t.includes('Saved to Google Contacts')));
  assert.ok(sent(calls).some((t) => t.includes('href="https://sg.linkedin.com/in/tanweiming"')));
});

test('VIP path drafts a note and a second-meeting ask; intel failure is reported and can be retried', async () => {
  const { env, calls, llm, state } = setup();
  state.failIntel = true;
  const id = await saveCard(env);
  assert.ok(sent(calls).some((t) => /Couldn't research Tan Wei Ming/.test(t)));

  await post(env, text('/skip'));
  await post(env, text('/skip'));
  await post(env, text('/skip'));
  await post(env, tap('vip:yes'));
  assert.match(lastText(calls), /VIP path for Tan Wei Ming/);
  assert.equal(llm.filter((l) => l.useCase === 'card-followup').length, 2);
  assert.equal((await rows('select is_vip from card_connect.contacts'))[0].is_vip, true);

  state.failIntel = false;
  await post(env, tap(`intel:${id}`));
  assert.match(lastText(calls), /^🔍 <b>Intel — Tan Wei Ming/);
  assert.equal((await rows('select intel from card_connect.contacts'))[0].intel.pain_points.length, 3);
});

test('saving a second card mid-questions restarts the questions for the new card', async () => {
  const { env } = setup();
  const first = await saveCard(env);
  await post(env, text('about the first'));
  const second = await saveCard(env);
  const [conv] = await rows('select contact_id, step from card_connect.conversations');
  assert.deepEqual([Number(conv.contact_id), conv.step], [second, 'topic']);
  await post(env, text('about the second'));
  const byId = Object.fromEntries((await rows('select id, topic from card_connect.contacts')).map((r) => [Number(r.id), r.topic]));
  assert.deepEqual(byId, { [first]: 'about the first', [second]: 'about the second' });
});

test('stage, snooze, notes and delete buttons; another chat cannot touch the contact', async () => {
  const { env, calls } = setup();
  const id = await saveCard(env);
  await post(env, text('/cancel'));

  await post(env, tap(`stage:${id}:meeting`));
  assert.match(calls.find((c) => c.method === 'answerCallbackQuery' && c.body.text?.includes('Meeting')).body.text, /next follow-up in 2d/);
  await post(env, tap(`stage:${id}:won`));
  let [c] = await rows('select * from card_connect.contacts');
  assert.deepEqual([c.stage, c.next_followup_at], ['won', null]);

  await post(env, tap(`stage:${id}:proposal`));
  await post(env, tap(`snooze:${id}`));
  [c] = await rows('select * from card_connect.contacts');
  assert.ok(Math.abs(c.next_followup_at - Date.now() - 3 * 86400000) < 60000);

  await post(env, tap(`note:${id}`));
  await post(env, text('prefers WhatsApp'));
  await post(env, tap(`note:${id}`));
  await post(env, text('budget in Q1'));
  assert.equal((await rows('select notes from card_connect.contacts'))[0].notes, 'prefers WhatsApp\nbudget in Q1');
  await post(env, text('just chatting'));
  assert.match(lastText(calls), /Send me a photo/);

  const other = tap(`delete:${id}`, 43);
  other.callback_query.message.chat.id = 43;
  await post(env, other);
  assert.equal((await rows('select 1 from card_connect.contacts')).length, 1);
  await post(env, tap(`delete:${id}`));
  assert.equal((await rows('select 1 from card_connect.contacts')).length, 0);
});

test('/pipeline, /list, /search, /show, /delete', async () => {
  const { env, calls } = setup();
  await post(env, text('/pipeline'));
  assert.match(lastText(calls), /No contacts yet/);
  const id = await saveCard(env);
  await post(env, text('/cancel'));

  await post(env, text('/pipeline'));
  assert.match(lastText(calls), /🆕 New — <b>1<\/b>/);
  await post(env, text('/list'));
  assert.match(lastText(calls), new RegExp(`Tan Wei Ming</b> — Acme &lt;Pte&gt; Ltd 🆕 New <code>${id}</code>`));
  await post(env, text('/search acme.sg'));
  assert.match(lastText(calls), /Tan Wei Ming/);
  await post(env, text('/search 9123'));
  assert.match(lastText(calls), /Tan Wei Ming/);
  await post(env, text('/search %'));
  assert.match(lastText(calls), /No results/);
  await post(env, text(`/show ${id}`));
  assert.ok(calls.at(-1).body.reply_markup.inline_keyboard[0][0].callback_data.startsWith(`stage:${id}:`));
  await post(env, text('/show abc'));
  assert.match(lastText(calls), /Usage: \/show/);
  await post(env, text(`/delete ${id}`));
  assert.match(lastText(calls), /deleted/);
  await post(env, text(`/delete ${id}`));
  assert.match(lastText(calls), /No contact with that id/);
});

test('daily run: due follow-up cards are sent once and pushed out; day-3 and day-14 drafts go out once', async () => {
  const { env, calls, llm } = setup();
  const id = await saveCard(env);
  await post(env, text('/followups'));
  assert.match(lastText(calls), /No follow-ups due/);

  await rows(`update card_connect.contacts set next_followup_at = now() - interval '1 hour', linkedin_nudge_at = now() - interval '1 hour', second_touch_at = now() - interval '1 hour'`);
  await post(env, text('/followups'));
  assert.match(lastText(calls), /Follow up: Tan Wei Ming/);
  assert.ok((await rows('select next_followup_at from card_connect.contacts'))[0].next_followup_at < new Date(), '/followups does not reschedule');

  calls.length = 0; llm.length = 0;
  await worker.scheduled({}, env);
  const texts = sent(calls);
  assert.ok(texts.some((t) => t.includes('1 follow-up due')));
  assert.ok(texts.some((t) => t.includes('Follow up: Tan Wei Ming') && t.includes('Manual ops')));
  assert.ok(texts.some((t) => t.includes('Day 3 nudge')));
  assert.ok(texts.some((t) => t.includes('Day 14 check-in')));
  assert.equal(llm.length, 2);
  const [c] = await rows('select * from card_connect.contacts');
  assert.ok(c.next_followup_at > new Date());
  assert.deepEqual([c.linkedin_nudge_at, c.second_touch_at], [null, null]);

  calls.length = 0;
  await runReminders(env);
  assert.equal(calls.length, 0, 'nothing is due the second time');
  assert.equal(id, Number(c.id));
});

test('the day-14 draft is dropped, not sent, once the contact has moved past Contacted; a failed draft is retried', async () => {
  const { env, calls, llm } = setup();
  const id = await saveCard(env);
  await post(env, tap(`stage:${id}:meeting`));
  await rows(`update card_connect.contacts set second_touch_at = now() - interval '1 hour'`);
  calls.length = 0; llm.length = 0;
  await runReminders(env);
  assert.equal(llm.length, 0);
  assert.equal((await rows('select second_touch_at from card_connect.contacts'))[0].second_touch_at, null);

  await rows(`update card_connect.contacts set linkedin_nudge_at = now() - interval '1 hour'`);
  const broken = { ...env, LLM: { fetch: async () => Response.json({ error: { code: 'down' } }, { status: 502 }) } };
  await runReminders(broken);
  assert.notEqual((await rows('select linkedin_nudge_at from card_connect.contacts'))[0].linkedin_nudge_at, null);
});

test('stale pending cards and abandoned conversations are pruned', async () => {
  const { env } = setup();
  await saveCard(env);
  await post(env, photo());
  await rows(`update card_connect.pending set created_at = now() - interval '8 days'`);
  await rows(`update card_connect.conversations set updated_at = now() - interval '2 days'`);
  await worker.scheduled({}, env);
  assert.equal((await rows('select 1 from card_connect.pending')).length, 0);
  assert.equal((await rows('select 1 from card_connect.conversations')).length, 0);
});

test('/healthz passes with Postgres and the gateway binding, fails without the binding', async () => {
  const { env } = setup();
  assert.equal((await worker.fetch(new Request('https://x/healthz'), env, { waitUntil() {} })).status, 200);
  assert.equal((await worker.fetch(new Request('https://x/healthz'), { ...env, LLM: undefined }, { waitUntil() {} })).status, 500);
  const page = await worker.fetch(new Request('https://x/'), env, { waitUntil() {} });
  assert.match(await page.text(), /Card Connect/);
});
