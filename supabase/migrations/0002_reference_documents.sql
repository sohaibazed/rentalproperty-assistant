-- Text of the owner's reference documents (leases, LLC papers, notes), so the chat can answer
-- questions from them. Filled by reference/supabase-documents.sql, which `make reference-sql`
-- builds from the reference/ folder (tenant-screening/ is never included).
-- Run after 0001_init.sql. Safe to run again.

create table if not exists reference_documents (
  id          bigint generated always as identity primary key,
  path        text not null unique,          -- e.g. 'leases/2026_lease.md'
  category    text not null check (category in ('lease', 'llc', 'notes')),
  title       text not null,
  pages       integer,                       -- PDF page count; null for markdown
  content     text not null,
  updated_at  timestamptz not null default now()
);

-- The app only reads these; you load them yourself in the SQL Editor.
grant select on reference_documents to rental_agent;

alter table reference_documents enable row level security;
drop policy if exists rental_agent_read on reference_documents;
create policy rental_agent_read on reference_documents for select to rental_agent using (true);
