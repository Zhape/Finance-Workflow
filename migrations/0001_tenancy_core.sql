-- Tenancy core for finance-workflows.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration `tenancy_core`.
--
-- Every org-owned table carries org_id and is protected by RLS keyed on
-- membership. The worker connects with the service role (which bypasses RLS)
-- and filters by org_id in code; these policies are the second line of
-- defence, so that a leaked publishable key cannot read across orgs.
--
-- Writes are deliberately NOT granted to `authenticated`: all mutation goes
-- through the worker API, which is where approval rules and audit live.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Orgs and membership
-- ---------------------------------------------------------------------------

create table public.orgs (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_at  timestamptz not null default now()
);

create type public.org_role as enum ('admin', 'member', 'viewer');

create table public.org_members (
  org_id      uuid not null references public.orgs(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  email       text not null,
  role        public.org_role not null default 'member',
  created_at  timestamptz not null default now(),
  primary key (org_id, user_id)
);

create index on public.org_members (user_id);

-- ---------------------------------------------------------------------------
-- Integration connections (encrypted OAuth tokens)
-- ---------------------------------------------------------------------------

create table public.connections (
  id            uuid primary key default gen_random_uuid(),
  org_id        uuid not null references public.orgs(id) on delete cascade,
  provider      text not null,
  name          text not null,
  label         text,
  tenant_id     text,
  tenant_name   text,
  -- Fernet ciphertext of the token payload. Never plaintext, and never
  -- readable by `authenticated` (column grants below).
  secret        bytea not null,
  key_id        text not null,
  connected_by  text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (org_id, provider, name)
);

-- Short-lived PKCE state. The verifier stays server-side; the browser only
-- ever carries the opaque state value.
create table public.oauth_states (
  state       text primary key,
  org_id      uuid not null references public.orgs(id) on delete cascade,
  provider    text not null,
  name        text not null,
  verifier    text not null,
  created_by  text,
  expires_at  timestamptz not null
);

create index on public.oauth_states (expires_at);

-- ---------------------------------------------------------------------------
-- Org reference data
-- ---------------------------------------------------------------------------

create table public.vendor_bank_details (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.orgs(id) on delete cascade,
  region      text not null,
  vendor      text not null,
  vendor_key  text not null,
  fields      jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now(),
  unique (org_id, region, vendor_key)
);

create index on public.vendor_bank_details (org_id, region, vendor_key);

create table public.bank_layouts (
  org_id      uuid not null references public.orgs(id) on delete cascade,
  region      text not null,
  columns     jsonb not null,
  updated_at  timestamptz not null default now(),
  primary key (org_id, region)
);

-- ---------------------------------------------------------------------------
-- Runs
-- ---------------------------------------------------------------------------

create table public.runs (
  id             uuid primary key default gen_random_uuid(),
  org_id         uuid not null references public.orgs(id) on delete cascade,
  workflow       text not null,
  params         jsonb not null default '{}'::jsonb,
  status         text not null,
  summary        text not null default '',
  error          text,
  columns        jsonb not null default '[]'::jsonb,
  rows           jsonb not null default '[]'::jsonb,
  warnings       jsonb not null default '[]'::jsonb,
  log            jsonb not null default '[]'::jsonb,
  approved_ids   jsonb not null default '[]'::jsonb,
  created_by     text not null,
  created_at     timestamptz not null default now(),
  approved_by    text,
  approved_at    timestamptz,
  artifact_name  text,
  artifact       bytea,
  constraint runs_status_valid
    check (status in ('empty', 'needs_approval', 'complete', 'failed'))
);

create index on public.runs (org_id, created_at desc);

-- A completed run must carry who approved it and when. Approval is the
-- product's core control; an unattributed completion is a bug, not a state.
alter table public.runs
  add constraint runs_approval_recorded
  check (
    status <> 'complete'
    or (approved_by is not null and approved_at is not null)
  );

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table public.orgs                enable row level security;
alter table public.org_members         enable row level security;
alter table public.connections         enable row level security;
alter table public.oauth_states        enable row level security;
alter table public.vendor_bank_details enable row level security;
alter table public.bank_layouts        enable row level security;
alter table public.runs                enable row level security;

-- Policies are (re)created in 0002 against private.is_org_member.
-- No insert/update/delete policies anywhere: mutation is the worker's job.

-- ---------------------------------------------------------------------------
-- Column grants
-- ---------------------------------------------------------------------------
-- RLS decides which ROWS a session sees; these decide which COLUMNS. Even a
-- legitimate member session must never be able to read a stored token.

revoke all on public.connections from anon, authenticated;
grant select (id, org_id, provider, name, label, tenant_id, tenant_name,
              connected_by, created_at, updated_at)
  on public.connections to authenticated;

revoke all on public.oauth_states from anon, authenticated;
