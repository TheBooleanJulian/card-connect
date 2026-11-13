// node --test   The pure logic in core.js: card validation, pipeline formatting, intel shaping.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  contactContext, contactKeyboard, describeDue, finaliseIntel, formatContact, formatIntel, formatPipeline, formatReminder,
  intelContext, normaliseCard, parseJson, parseSearchResults, whatsappLink
} from '../core.js';
import { personBody } from '../google.js';

const NOW = new Date('2026-11-14T01:00:00Z');
const days = (n) => new Date(NOW.getTime() + n * 86400000);

test('parseJson tolerates fences and prose, rejects non-objects', () => {
  assert.deepEqual(parseJson('```json\n{"a": 1}\n```'), { a: 1 });
  assert.deepEqual(parseJson('Sure! {"a": {"b": 2}} hope that helps'), { a: { b: 2 } });
  assert.throws(() => parseJson('no json here'));
  assert.throws(() => parseJson('{broken'));
});

test('normaliseCard keeps printed fields, coerces lists, rejects non-cards', () => {
  const card = normaliseCard({ full_name: '  Tan Wei Ming ', phones: '+6591234567', emails: ['a@b.sg', '', null], title: 42, website: {}, extra: 'x' });
  assert.deepEqual(card, { full_name: 'Tan Wei Ming', title: '42', phones: ['+6591234567'], emails: ['a@b.sg'] });
  assert.equal(normaliseCard({ error: 'NOT_A_NAMECARD' }), null);
  assert.equal(normaliseCard({ organization: 'No Name Pte Ltd' }), null);
});

test('whatsappLink needs a real number and greets by first name', () => {
  assert.equal(whatsappLink('12345', 'Tan'), null);
  const link = whatsappLink('+65 9123 4567', 'Tan Wei Ming');
  assert.ok(link.startsWith('https://wa.me/6591234567?text=Hi%20Tan%2C'));
});

test('describeDue', () => {
  assert.equal(describeDue(days(-1), NOW), 'due now');
  assert.equal(describeDue(days(0.5), NOW), 'within 24h');
  assert.equal(describeDue(days(3), NOW), 'in 3d');
  assert.equal(describeDue(null, NOW), null);
});

test('formatters escape what came off the card or out of the model', () => {
  const contact = {
    id: 7, full_name: 'A <b>B</b>', organization: 'X & Y', phones: ['+65 1'], emails: [], stage: 'contacted',
    topic: '<script>', next_followup_at: days(2), created_at: days(-3), stage_updated_at: days(-1),
    intel: { pain_points: [{ pain: 'Slow <ops>', opportunity: 'Bots & more' }] }, followup_opener: 'Hi <there>'
  };
  for (const text of [formatContact(contact, NOW), formatReminder(contact, NOW), formatPipeline([contact], NOW)]) {
    assert.ok(!text.includes('<script>') && !text.includes('A <b>B</b>') && !text.includes('X & Y'), text);
  }
  assert.match(formatContact(contact, NOW), /Next follow-up: in 2d/);
  assert.match(formatReminder(contact, NOW), /scanned 3 days ago · last update 1 day ago/);
  assert.match(formatReminder(contact, NOW), /Slow &lt;ops&gt;/);
});

test('formatPipeline counts stages and flags overdue', () => {
  const text = formatPipeline([
    { full_name: 'A', stage: 'new', next_followup_at: days(-1) },
    { full_name: 'B', stage: 'new', next_followup_at: days(1) },
    { full_name: 'C', stage: 'won' },
    { full_name: 'D', stage: 'bogus' }
  ], NOW);
  assert.match(text, /🆕 New — <b>3<\/b>/);
  assert.match(text, /• A ⏰/);
  assert.match(text, /🏆 Won — <b>1<\/b>/);
  assert.match(text, /1 follow-up due/);
});

test('contactKeyboard ticks the stage and drops Snooze once closed', () => {
  const open = contactKeyboard(5, 'meeting');
  assert.equal(open.inline_keyboard[0][2].text, '✓ 🤝 Meeting');
  assert.ok(open.inline_keyboard[2].some((b) => b.callback_data === 'snooze:5'));
  assert.ok(!contactKeyboard(5, 'won').inline_keyboard[2].some((b) => b.callback_data === 'snooze:5'));
});

test('contactContext leaves the request out when asked', () => {
  const c = { full_name: 'Tan', title: 'CTO', organization: 'Acme', topic: 'bots', request: 'an intro', notes: 'keen' };
  assert.match(contactContext(c), /Role: CTO at Acme\nTopic discussed: bots\nTheir request: an intro\nJulian's notes: keen/);
  assert.ok(!contactContext(c, false).includes('an intro'));
});

test('search results: first linkedin.com/in link, snippets joined', () => {
  const found = parseSearchResults({ organic: [
    { link: 'https://acme.sg/team', title: 'Team', snippet: 'Tan is CTO' },
    { link: 'https://sg.linkedin.com/in/tan?trk=x', title: 'Tan - CTO' },
    { link: 'https://linkedin.com/in/other' }
  ] });
  assert.equal(found.linkedinUrl, 'https://sg.linkedin.com/in/tan');
  assert.match(found.snippets, /Team — Tan is CTO/);
  assert.deepEqual(parseSearchResults({}), { linkedinUrl: null, snippets: '' });
});

test('intel without search results is forced to low confidence and never keeps a model-made URL', () => {
  const intel = finaliseIntel({ linkedin_url: 'https://linkedin.com/in/guessed', confidence: 'high', data_source: 'search_snippets' });
  assert.deepEqual([intel.linkedin_url, intel.confidence, intel.data_source], [null, 'low', 'namecard_only']);
  const found = { linkedinUrl: 'https://linkedin.com/in/real', snippets: 'x' };
  assert.equal(finaliseIntel({ linkedin_url: 'https://linkedin.com/in/guessed', confidence: 'high' }, found).linkedin_url, 'https://linkedin.com/in/real');
  assert.match(intelContext({ full_name: 'Tan', topic: 'bots' }), /JULIAN'S NOTES:\nTalked about: bots[\s\S]*No search results available/);
});

test('formatIntel only links https URLs', () => {
  const base = { confidence: 'low', data_source: 'namecard_only', pain_points: [{ pain: 'P', detail: 'D', opportunity: 'O' }], follow_up_opener: 'Hi' };
  assert.ok(!formatIntel({ ...base, linkedin_url: 'javascript:alert(1)' }, 'Tan').includes('href'));
  assert.match(formatIntel({ ...base, linkedin_url: 'https://linkedin.com/in/tan' }, 'Tan'), /href="https:\/\/linkedin.com\/in\/tan"/);
  assert.match(formatIntel(base, 'Tan'), /inferred from the card alone/);
});

test('Google person body', () => {
  const body = personBody({ full_name: 'Tan Wei Ming', organization: 'Acme', phones: ['+651'], emails: ['a@b.sg'], website: 'acme.sg' });
  assert.deepEqual(body.names, [{ givenName: 'Tan Wei', familyName: 'Ming' }]);
  assert.deepEqual(body.organizations, [{ name: 'Acme', title: '' }]);
  assert.deepEqual(body.phoneNumbers, [{ value: '+651' }]);
  assert.equal(body.addresses, undefined);
});
