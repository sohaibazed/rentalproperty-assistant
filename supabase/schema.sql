-- Rental Agent database schema. Run this whole file once in the Supabase SQL Editor
-- (see README section 3). It is safe to run again: it skips anything that already exists.
--
-- Tables: LLCs, properties, tenants, leases, rent payments, expenses, rent research, the owner's
-- documents, email-sync and job bookkeeping, and chat history. The app logs in as the
-- restricted role rental_agent (created at the end), which can read, add and update rows but
-- never delete them.

-- Schedule E (Form 1040) expense lines, plus 'other'.
do $$ begin
  create type schedule_e_category as enum (
    'advertising', 'auto_travel', 'cleaning_maintenance', 'commissions', 'insurance',
    'legal_professional', 'management_fees', 'mortgage_interest', 'other_interest',
    'repairs', 'supplies', 'taxes', 'utilities', 'depreciation', 'other'
  );
exception when duplicate_object then null; end $$;

-- LLCs that own the properties. Filled by the import-document skill from the certificate of
-- formation, operating agreement and EIN letter.
create table if not exists llcs (
  id                 bigint generated always as identity primary key,
  name               text not null,
  state              text not null default 'TX',
  file_number        text,                    -- Secretary of State filing number
  ein                text check (ein ~ '^\d{2}-\d{7}$'),
  formed_on          date,
  management         text check (management in ('managers', 'members')),
  members            jsonb not null default '[]',   -- [{"name": "...", "percent": 50}]
  managers           text[] not null default '{}',
  registered_agent   text,
  registered_office  text,
  mailing_address    text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create unique index if not exists llcs_name_key on llcs (lower(name));

create table if not exists properties (
  id             bigint generated always as identity primary key,
  address        text not null,
  unit           text,
  city           text,
  county         text,
  state          text not null default 'TX',
  zip            text,
  landlord_names text[],                  -- names as they appear on leases
  property_type  text,                    -- e.g. 'single-family house', 'townhome'
  beds           numeric(3,1),
  baths          numeric(3,1),
  sqft           integer,
  purchase_date  date,
  cost_basis     numeric(12,2),           -- for depreciation
  llc_id         bigint references llcs(id),  -- the LLC that owns it
  created_at     timestamptz not null default now(),
  unique (address, unit)
);

create table if not exists tenants (
  id          bigint generated always as identity primary key,
  full_name   text not null,
  email       text,
  phone       text,
  created_at  timestamptz not null default now()
);

-- One row per lease term: the original lease, then one per yearly extension.
create table if not exists leases (
  id                   bigint generated always as identity primary key,
  property_id          bigint not null references properties(id),
  start_date           date not null,
  end_date             date not null check (end_date > start_date),
  monthly_rent         numeric(10,2) not null check (monthly_rent > 0),
  deposit              numeric(10,2) check (deposit >= 0),
  original_lease_date  date,             -- set on extensions: date of the lease being extended
  other_terms          text,
  docx_path            text,
  pdf_path             text,
  created_at           timestamptz not null default now()
);

-- Co-tenants on each lease term.
create table if not exists lease_tenants (
  lease_id   bigint not null references leases(id),
  tenant_id  bigint not null references tenants(id),
  primary key (lease_id, tenant_id)
);

create table if not exists rent_payments (
  id                 bigint generated always as identity primary key,
  lease_id           bigint references leases(id),     -- null until matched
  amount             numeric(10,2) not null check (amount > 0),
  paid_on            date not null,
  payer              text,
  source_message_id  text unique,                      -- Gmail message id; null for manual entries
  needs_review       boolean not null default false,
  created_at         timestamptz not null default now()
);

create table if not exists expenses (
  id                   bigint generated always as identity primary key,
  property_id          bigint references properties(id),  -- null = LLC-wide expense
  amount               numeric(10,2) not null check (amount > 0),
  spent_on             date not null,
  vendor               text,
  description          text,
  schedule_e_category  schedule_e_category not null,
  receipt_path         text,
  source_message_id    text unique,
  needs_review         boolean not null default false,
  created_at           timestamptz not null default now()
);

create table if not exists rent_research (
  id                bigint generated always as identity primary key,
  property_id       bigint not null references properties(id),
  year              integer not null,
  comps             jsonb not null,
  recommended_rent  numeric(10,2),
  rationale         text,
  report_path       text,
  created_at        timestamptz not null default now(),
  unique (property_id, year)
);

-- One row per Gmail message looked at, so syncs can be re-run safely.
create table if not exists email_ingest_log (
  message_id    text primary key,
  processed_at  timestamptz not null default now(),
  outcome       text not null check (outcome in ('rent', 'expense', 'ignored'))
);

-- One row per sync run; the next run searches from the last successful run's start (minus overlap).
create table if not exists sync_runs (
  id           bigint generated always as identity primary key,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  status       text not null default 'running' check (status in ('running', 'success', 'failed')),
  inserted     integer not null default 0
);

-- One row per job run started from the web app or its scheduler.
create table if not exists job_runs (
  id           bigint generated always as identity primary key,
  job          text not null,
  params       jsonb not null default '{}',
  trigger      text not null default 'manual' check (trigger in ('manual', 'schedule')),
  status       text not null default 'running' check (status in ('running', 'success', 'failed')),
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  summary      text,
  outputs      text[],                  -- paths relative to artifacts/
  log          text
);

-- What each Claude call cost, as reported by the Agent SDK (list-price estimate, not the
-- invoice). The dashboard sums the current month.
create table if not exists claude_usage (
  id        bigint generated always as identity primary key,
  used_at   timestamptz not null default now(),
  source    text not null,                -- 'chat', 'email-extraction', 'rent-research'
  model     text not null,
  cost_usd  numeric(12,6) not null
);
create index if not exists claude_usage_used_at on claude_usage (used_at);

-- Chat history. Proposals are actions the assistant suggested (save an expense, run a job,
-- import a document, add a memory rule); they only take effect when the owner clicks to confirm.
create table if not exists chats (
  id              bigint generated always as identity primary key,
  title           text not null,
  model           text not null,
  sdk_session_id  text,                 -- lets a reopened chat continue with full context
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table if not exists chat_messages (
  id           bigint generated always as identity primary key,
  chat_id      bigint not null references chats(id),
  role         text not null check (role in ('user', 'assistant')),
  content      text not null,
  attachments  text[],                  -- paths relative to artifacts/
  proposals    jsonb not null default '[]',
  model        text,
  created_at   timestamptz not null default now()
);
create index if not exists chat_messages_chat_id on chat_messages (chat_id, id);

-- Text of the owner's lease and LLC documents, so the chat can answer questions from them.
-- Written by the import-document skill (and optionally `make reference-sql`).
create table if not exists reference_documents (
  id          bigint generated always as identity primary key,
  path        text not null unique,          -- e.g. 'imports/2027_lease.pdf'
  category    text not null check (category in ('lease', 'llc', 'notes')),
  title       text not null,
  pages       integer,                       -- PDF page count; null for markdown
  content     text not null,
  updated_at  timestamptz not null default now()
);

-- Least-privilege role for the app. Give it a password separately, never in this file
-- (README section 3.3):
--   alter role rental_agent with login password '...';
do $$ begin
  create role rental_agent nologin;
exception when duplicate_object then null; end $$;

grant usage on schema public to rental_agent;
grant usage on type schedule_e_category to rental_agent;
grant select, insert on
  llcs, properties, tenants, leases, lease_tenants, rent_payments, expenses, rent_research,
  email_ingest_log, sync_runs, job_runs, chats, chat_messages, reference_documents, claude_usage
  to rental_agent;
grant update on
  llcs, properties, leases, rent_payments, expenses, rent_research, sync_runs, job_runs, chats,
  chat_messages, reference_documents
  to rental_agent;  -- no DELETE anywhere

-- Row-level security on every table. Only rental_agent gets a policy, so Supabase's public
-- API roles (anon, authenticated) see nothing.
do $$
declare t text;
begin
  foreach t in array array[
    'llcs', 'properties', 'tenants', 'leases', 'lease_tenants', 'rent_payments', 'expenses',
    'rent_research', 'email_ingest_log', 'sync_runs', 'job_runs', 'chats', 'chat_messages',
    'reference_documents', 'claude_usage'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists rental_agent_all on %I', t);
    execute format('create policy rental_agent_all on %I to rental_agent '
                   'using (true) with check (true)', t);
  end loop;
end $$;
