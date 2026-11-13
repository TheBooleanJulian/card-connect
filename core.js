// Card Connect's logic with no I/O: card validation, pipeline rules, intel shaping and every message format.
// Everything here is plain functions over plain objects, so it is tested without Telegram, Postgres or the gateway.
// All formatters return Telegram HTML.

export const STAGES = ['new', 'contacted', 'meeting', 'proposal', 'won', 'lost'];
export const ACTIVE_STAGES = ['new', 'contacted', 'meeting', 'proposal'];

export const STAGE_LABEL = {
  new: '🆕 New',
  contacted: '💬 Contacted',
  meeting: '🤝 Meeting',
  proposal: '📄 Proposal',
  won: '🏆 Won',
  lost: '💤 Lost'
};

// Days until the next nudge once a contact enters a stage (null = pipeline closed)
export const FOLLOWUP_DAYS = { new: 2, contacted: 3, meeting: 2, proposal: 4, won: null, lost: null };

// Used by the Snooze button and as the re-nag interval after a reminder is sent
export const SNOOZE_DAYS = 3;

// One-off drafts after a card is saved: a LinkedIn comment on day 3, and a low-pressure "quick update"
// email on day 14, which only makes sense while there has been no real reply yet.
export const LINKEDIN_NUDGE_DAYS = 3;
export const SECOND_TOUCH_DAYS = 14;
export const SECOND_TOUCH_STAGES = ['new', 'contacted'];

export const esc = (value) =>
  String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// ── model replies ──────────────────────────────────────────────────────────

// Models wrap JSON in fences or prose however firmly they are told not to. Throws on anything that isn't an object.
export function parseJson(text) {
  const raw = String(text || '').trim();
  const start = raw.indexOf('{');
  const end = raw.lastIndexOf('}');
  if (start === -1 || end <= start) throw new Error('model reply had no JSON object');
  const data = JSON.parse(raw.slice(start, end + 1));
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('model reply was not a JSON object');
  return data;
}

const TEXT_FIELDS = ['full_name', 'organization', 'title', 'website', 'address', 'linkedin'];
const LIST_FIELDS = ['phones', 'emails'];
const scalar = (v) => (typeof v === 'string' || typeof v === 'number') && String(v).trim() !== '';

// Coerce the model's JSON into the shape the rest of the bot relies on: text fields are non-empty strings,
// phones/emails are lists of strings, anything else is dropped. Returns null when it isn't a usable name card.
export function normaliseCard(data) {
  if (!data || data.error) return null;
  const card = {};
  for (const field of TEXT_FIELDS) {
    if (scalar(data[field])) card[field] = String(data[field]).trim();
  }
  for (const field of LIST_FIELDS) {
    const value = scalar(data[field]) ? [data[field]] : data[field];
    if (Array.isArray(value)) {
      const items = value.filter(scalar).map((v) => String(v).trim());
      if (items.length) card[field] = items;
    }
  }
  return card.full_name ? card : null;
}

// ── time ───────────────────────────────────────────────────────────────────

const DAY_MS = 86400000;

export function ts(value) {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function describeDue(value, now = new Date()) {
  const due = ts(value);
  if (!due) return null;
  const ms = due - now;
  if (ms <= 0) return 'due now';
  if (ms < DAY_MS) return 'within 24h';
  return `in ${Math.round(ms / DAY_MS)}d`;
}

function ago(date, now) {
  const days = Math.floor((now - date) / DAY_MS);
  if (days <= 0) return 'today';
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

// ── messages ───────────────────────────────────────────────────────────────

export const roleLine = (c) => [c.title, c.organization].filter(Boolean).join(' — ');

// What was read off the card, shown before anything is saved.
export function formatCardSummary(card) {
  const lines = [`📇 <b>${esc(card.full_name || 'Unknown name')}</b>`];
  if (roleLine(card)) lines.push(esc(roleLine(card)));
  for (const p of card.phones || []) lines.push(`📞 ${esc(p)}`);
  for (const e of card.emails || []) lines.push(`✉️ ${esc(e)}`);
  if (card.website) lines.push(`🌐 ${esc(card.website)}`);
  if (card.address) lines.push(`📍 ${esc(card.address)}`);
  if (card.linkedin) lines.push(`🔗 ${esc(card.linkedin)}`);
  return lines.join('\n');
}

const painPoints = (contact) =>
  (contact.intel?.pain_points || []).filter((p) => p && typeof p === 'object' && p.pain);

// Full saved-contact card: details, notes, pipeline state, intel summary.
export function formatContact(contact, now = new Date()) {
  const lines = [formatCardSummary(contact)];
  for (const [label, key] of [['Talked about', 'topic'], ['They asked for', 'request'], ['Notes', 'notes']]) {
    if (contact[key]) lines.push(`📝 <b>${label}:</b> ${esc(contact[key])}`);
  }
  if (contact.is_vip) lines.push('🌟 VIP');

  const stage = contact.stage || 'new';
  lines.push('', `📊 Stage: ${STAGE_LABEL[stage] || esc(stage)}`);
  const due = ACTIVE_STAGES.includes(stage) ? describeDue(contact.next_followup_at, now) : null;
  if (due) lines.push(`⏰ Next follow-up: ${due}`);

  const pains = painPoints(contact);
  if (pains.length) lines.push('', '🎯 <b>Pain points</b>', ...pains.slice(0, 3).map((p) => `  • ${esc(p.pain)}`));

  if (contact.id != null) lines.push('', `🆔 <code>${esc(contact.id)}</code>`);
  return lines.join('\n');
}

// Follow-up card: who, where they are in the pipeline, what to say.
export function formatReminder(contact, now = new Date()) {
  const stage = contact.stage || 'new';
  const lines = [`⏰ <b>Follow up: ${esc(contact.full_name || 'Unknown')}</b>`];
  if (roleLine(contact)) lines.push(esc(roleLine(contact)));

  const meta = [STAGE_LABEL[stage] || stage];
  const scanned = ts(contact.created_at);
  if (scanned) meta.push(`scanned ${ago(scanned, now)}`);
  const changed = ts(contact.stage_updated_at);
  if (changed && (!scanned || Math.abs(changed - scanned) > 60000)) meta.push(`last update ${ago(changed, now)}`);
  lines.push(meta.map(esc).join(' · '));

  const reach = [...(contact.phones || []).slice(0, 1), ...(contact.emails || []).slice(0, 1)];
  if (reach.length) lines.push('📞 ' + reach.map(esc).join('  ·  '));
  if (contact.topic) lines.push(`📝 ${esc(contact.topic)}`);

  const top = painPoints(contact)[0];
  if (top) {
    lines.push('', `🎯 <b>${esc(top.pain)}</b>`);
    if (top.opportunity) lines.push(`💡 ${esc(top.opportunity)}`);
  }
  if (contact.followup_opener) lines.push('', '✉️ <b>Suggested opener</b>', `<i>${esc(contact.followup_opener)}</i>`);
  return lines.join('\n');
}

// Stage-by-stage overview with overdue follow-ups flagged.
export function formatPipeline(contacts, now = new Date()) {
  const byStage = Object.fromEntries(STAGES.map((s) => [s, []]));
  for (const c of contacts) byStage[STAGES.includes(c.stage) ? c.stage : 'new'].push(c);

  let overdue = 0;
  const lines = ['📊 <b>Pipeline</b>', ''];
  for (const s of STAGES) {
    const items = byStage[s];
    lines.push(`${STAGE_LABEL[s]} — <b>${items.length}</b>`);
    if (!ACTIVE_STAGES.includes(s)) continue;
    items.forEach((c, i) => {
      const due = ts(c.next_followup_at);
      const isDue = Boolean(due && due <= now);
      if (isDue) overdue += 1;
      if (i < 5) lines.push(`   • ${esc(c.full_name || 'Unknown')}${c.organization ? ` (${esc(c.organization)})` : ''}${isDue ? ' ⏰' : ''}`);
    });
    if (items.length > 5) lines.push(`   …and ${items.length - 5} more`);
  }
  if (overdue) lines.push('', `⏰ ${overdue} follow-up${overdue === 1 ? '' : 's'} due — /followups`);
  return lines.join('\n');
}

export const contactLines = (contacts) =>
  contacts.map((c) =>
    `• <b>${esc(c.full_name || 'Unknown')}</b>${c.organization ? ` — ${esc(c.organization)}` : ''} ${STAGE_LABEL[c.stage] || ''} <code>${esc(c.id)}</code>`);

// wa.me click-to-chat link with a greeting pre-filled, or null when the number is too short to be real.
export function whatsappLink(phone, name) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (digits.length < 8) return null;
  const first = String(name || '').trim().split(/\s+/)[0];
  const greeting = first ? `Hi ${first}, great meeting you — this is Julian!` : 'Hi, great meeting you!';
  return `https://wa.me/${digits}?text=${encodeURIComponent(greeting)}`;
}

// Stage buttons (current one ticked) + notes / intel / snooze / delete.
export function contactKeyboard(id, stage) {
  const current = STAGES.includes(stage) ? stage : 'new';
  const stages = STAGES.map((s) => ({ text: (s === current ? '✓ ' : '') + STAGE_LABEL[s], callback_data: `stage:${id}:${s}` }));
  const actions = [{ text: '✏️ Notes', callback_data: `note:${id}` }, { text: '🔍 Intel', callback_data: `intel:${id}` }];
  if (ACTIVE_STAGES.includes(current)) actions.push({ text: `😴 ${SNOOZE_DAYS}d`, callback_data: `snooze:${id}` });
  actions.push({ text: '🗑', callback_data: `delete:${id}` });
  return { inline_keyboard: [stages.slice(0, 3), stages.slice(3), actions] };
}

// ── drafts ─────────────────────────────────────────────────────────────────

// What the drafting prompts are told about a contact.
export function contactContext(contact, withRequest = true) {
  const lines = [`Name: ${contact.full_name || 'Unknown'}`];
  const role = [contact.title, contact.organization].filter(Boolean).join(' at ');
  if (role) lines.push(`Role: ${role}`);
  if (contact.topic) lines.push(`Topic discussed: ${contact.topic}`);
  if (withRequest && contact.request) lines.push(`Their request: ${contact.request}`);
  if (contact.notes) lines.push(`Julian's notes: ${contact.notes}`);
  return lines.join('\n');
}

// ── intel ──────────────────────────────────────────────────────────────────

export function searchQuery(contact) {
  if (!contact.full_name) return null;
  return `"${contact.full_name}"${contact.organization ? ` "${contact.organization}"` : ''} linkedin`;
}

// From a Serper response: the first linkedin.com/in/ result and the result snippets.
export function parseSearchResults(data) {
  let linkedinUrl = null;
  const snippets = [];
  for (const result of (data?.organic || []).slice(0, 5)) {
    const link = String(result.link || '');
    if (!linkedinUrl && link.includes('linkedin.com/in/')) linkedinUrl = link.split('?')[0];
    const parts = [result.title, result.snippet].filter(Boolean).map(String);
    if (parts.length) snippets.push(`${link}\n${parts.join(' — ')}`);
  }
  return { linkedinUrl, snippets: snippets.join('\n\n') };
}

export function intelContext(contact, { linkedinUrl = null, snippets = '' } = {}) {
  const lines = ['NAME CARD:', `Name: ${contact.full_name || 'Unknown'}`];
  for (const [label, key] of [['Title', 'title'], ['Company', 'organization'], ['Website', 'website'], ['Address', 'address']]) {
    if (contact[key]) lines.push(`${label}: ${contact[key]}`);
  }
  if (contact.emails?.length) lines.push(`Email: ${contact.emails[0]}`);

  const notes = [['Talked about', contact.topic], ['They asked for', contact.request], ['Notes', contact.notes]].filter(([, v]) => v);
  if (notes.length) lines.push('', "JULIAN'S NOTES:", ...notes.map(([label, v]) => `${label}: ${v}`));

  const known = linkedinUrl || contact.linkedin;
  if (known) lines.push('', `LINKEDIN: ${known}`);
  if (snippets) lines.push('', 'GOOGLE SEARCH RESULTS:', snippets.slice(0, 6000));
  else lines.push('', 'No search results available — use the name card only.');
  return lines.join('\n');
}

// Trust the search result over the model for the URL, and never keep one the model produced with nothing to
// copy it from. With no snippets the result is by definition a low-confidence guess from the card.
export function finaliseIntel(result, { linkedinUrl = null, snippets = '' } = {}) {
  const intel = { ...result, linkedin_url: linkedinUrl || (snippets ? result.linkedin_url || null : null) };
  if (!snippets) {
    intel.data_source = 'namecard_only';
    intel.confidence = 'low';
  }
  return intel;
}

export function formatIntel(result, name) {
  const lines = [`🔍 <b>Intel — ${esc(name)}</b>`];

  const confidence = String(result.confidence || 'unknown');
  const dot = { high: '🟢', medium: '🟡', low: '🔴' }[confidence] || '⚪';
  const source = { search_snippets: 'from public search results', namecard_only: 'inferred from the card alone' }[result.data_source] || '';
  lines.push(`${dot} Confidence: <b>${esc(confidence)}</b>${source ? ` — ${source}` : ''}`);

  const positions = (result.current_positions || []).filter((p) => p && typeof p === 'object');
  if (positions.length) {
    lines.push('', '💼 <b>Current positions</b>');
    for (const pos of positions.slice(0, 3)) {
      lines.push(`  • ${esc(pos.title || '')}${pos.company ? ` @ ${esc(pos.company)}` : ''}${pos.duration ? ` <i>${esc(pos.duration)}</i>` : ''}`);
    }
  }

  const pains = (result.pain_points || []).filter((p) => p && typeof p === 'object' && p.pain);
  if (pains.length) {
    lines.push('', '🎯 <b>Likely pain points &amp; openings</b>');
    pains.slice(0, 3).forEach((pain, i) => {
      lines.push('', `<b>${i + 1}. ${esc(pain.pain)}</b>`);
      if (pain.detail) lines.push(`<i>${esc(pain.detail)}</i>`);
      if (pain.opportunity) lines.push(`💡 ${esc(pain.opportunity)}`);
    });
  }

  if (result.follow_up_opener) lines.push('', '✉️ <b>Suggested opener</b>', `<i>${esc(result.follow_up_opener)}</i>`);

  const url = String(result.linkedin_url || '');
  if (url.startsWith('https://')) lines.push('', `🔗 <a href="${esc(url)}">LinkedIn profile</a>`);
  return lines.join('\n');
}
