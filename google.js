// Google Contacts over REST (People API, no SDK). Optional: without the GOOGLE_TOKEN_JSON secret, contacts are kept
// in the bot's own database only. The secret is the authorized-user JSON (client_id, client_secret, refresh_token)
// that authorize_google.py writes to token.json. A short-lived access token is fetched per call.

const API = 'https://people.googleapis.com/v1/people:createContact';

export const isLinked = (env) => Boolean(env.GOOGLE_TOKEN_JSON);

async function accessToken(env) {
  const t = JSON.parse(env.GOOGLE_TOKEN_JSON);
  const res = await fetch(t.token_uri || 'https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: t.client_id, client_secret: t.client_secret, refresh_token: t.refresh_token, grant_type: 'refresh_token' })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.access_token) throw new Error(`Google token refresh failed: ${res.status} ${data.error || ''}`.trim());
  return data.access_token;
}

// "Tan Wei Ming" -> given "Tan Wei", family "Ming" (same split the People API form uses for a single name field).
function splitName(fullName) {
  const parts = String(fullName || '').trim().split(/\s+/).filter(Boolean);
  if (parts.length <= 1) return { givenName: parts[0] || '', familyName: '' };
  return { givenName: parts.slice(0, -1).join(' '), familyName: parts[parts.length - 1] };
}

export function personBody(card) {
  const body = {};
  const name = splitName(card.full_name);
  if (name.givenName || name.familyName) body.names = [name];
  if (card.organization || card.title) body.organizations = [{ name: card.organization || '', title: card.title || '' }];
  if (card.phones?.length) body.phoneNumbers = card.phones.map((value) => ({ value }));
  if (card.emails?.length) body.emailAddresses = card.emails.map((value) => ({ value }));
  if (card.website) body.urls = [{ value: card.website }];
  if (card.address) body.addresses = [{ formattedValue: card.address }];
  body.biographies = [{ value: 'Added via Card Connect (Telegram bot)', contentType: 'TEXT_PLAIN' }];
  return body;
}

// Creates the contact and returns its People API resource name ("people/c123…").
export async function createContact(env, card) {
  const res = await fetch(API, {
    method: 'POST',
    headers: { Authorization: `Bearer ${await accessToken(env)}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(personBody(card))
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`Google Contacts create failed: ${res.status} ${data.error?.message || ''}`.trim());
  return data.resourceName || null;
}
