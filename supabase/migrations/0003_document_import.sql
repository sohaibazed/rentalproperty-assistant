-- LLCs, and the write access the import-document skill needs. Run after 0002. Safe to run again.
--
-- The import-document skill reads a new lease or LLC document, and after the owner confirms,
-- saves what it found: the LLC (this table), the property, tenants and lease term, and the
-- document's text (reference_documents).

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

alter table properties add column if not exists llc_id bigint references llcs(id);

grant select, insert, update on llcs to rental_agent;
grant update on properties to rental_agent;              -- link to an LLC, fill in details
grant insert, update on reference_documents to rental_agent;

alter table llcs enable row level security;
drop policy if exists rental_agent_all on llcs;
create policy rental_agent_all on llcs to rental_agent using (true) with check (true);

-- reference_documents was read-only to the app; imports now add and update rows (never delete).
drop policy if exists rental_agent_read on reference_documents;
drop policy if exists rental_agent_all on reference_documents;
create policy rental_agent_all on reference_documents to rental_agent using (true) with check (true);
