-- card-connect (Telegram name-card bot): saved contacts with their pipeline state and due dates, cards waiting for
-- Save/Discard, and the "which question am I on" state of the notes conversation.
-- Contacts are third-party personal data: only the app role can read them, and the schema is not exposed over the Data API.
-- App role: card_connect_app (password via scripts/provision.mjs card_connect).
--
-- This file is the copy the Worker's tests run against. The applied copy is
-- thebooleanjulian-db/supabase/migrations/20261114090000_card_connect.sql: keep the two identical.

create schema if not exists card_connect;

create table if not exists card_connect.contacts (
  id                   bigint generated always as identity primary key,
  chat_id              text not null,
  full_name            text,
  organization         text,
  title                text,
  phones               jsonb not null default '[]'::jsonb,
  emails               jsonb not null default '[]'::jsonb,
  website              text,
  address              text,
  linkedin             text,
  topic                text,           -- what was talked about
  request              text,           -- what they asked for
  notes                text,
  voice_file_id        text,           -- Telegram file id of the optional voice memo
  is_vip               boolean not null default false,
  google_resource_name text,           -- People API resource, when a Google account is linked
  stage                text not null default 'new',
  stage_updated_at     timestamptz not null default now(),
  next_followup_at     timestamptz,    -- null = nothing scheduled / pipeline closed
  linkedin_nudge_at    timestamptz,    -- null once the day-3 draft has been sent
  second_touch_at      timestamptz,    -- null once the day-14 draft has been sent
  followup_opener      text,
  intel                jsonb,
  created_at           timestamptz not null default now()
);
create index if not exists idx_card_connect_contacts_chat on card_connect.contacts (chat_id, id desc);
create index if not exists idx_card_connect_contacts_followup on card_connect.contacts (next_followup_at);

-- Cards read off a photo and not yet saved or discarded. Pruned after 7 days by the daily cron.
create table if not exists card_connect.pending (
  pid        text primary key,
  chat_id    text not null,
  card       jsonb not null,
  created_at timestamptz not null default now()
);

-- One conversation in flight per chat: the notes questions after a save (topic, request, voice, vip),
-- or "note" while the bot waits for the text of a note. Pruned after 1 day.
create table if not exists card_connect.conversations (
  chat_id    text primary key,
  contact_id bigint not null references card_connect.contacts (id) on delete cascade,
  step       text not null,
  updated_at timestamptz not null default now()
);

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'card_connect_app') then
    create role card_connect_app nologin;
  end if;
end $$;

revoke all on schema card_connect from public;
grant usage on schema card_connect to card_connect_app;
grant select, insert, update, delete on all tables in schema card_connect to card_connect_app;
alter default privileges in schema card_connect grant select, insert, update, delete on tables to card_connect_app;
