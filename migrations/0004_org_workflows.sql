-- Which workflows an organisation has.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration `org_workflows`.
--
-- Presence of a row means enabled. There is deliberately no "enabled boolean"
-- and no implicit default: a new org starts with nothing, and someone has to
-- decide what it gets. The alternative -- every org sees every workflow unless
-- excluded -- fails open, and the failure is an org seeing a workflow that
-- reads a system it should not touch.
--
-- The workflow key is text rather than a foreign key: workflows are code, not
-- rows. An unknown key here is inert, and the registry is the authority on
-- what a key means.
create table public.org_workflows (
  org_id       uuid not null references public.orgs(id) on delete cascade,
  workflow_key text not null,
  enabled_by   text,
  enabled_at   timestamptz not null default now(),
  primary key (org_id, workflow_key)
);

alter table public.org_workflows enable row level security;

create policy org_workflow_read on public.org_workflows
  for select to authenticated using (private.is_org_member(org_id));

revoke all on public.org_workflows from anon, authenticated;
grant select (org_id, workflow_key, enabled_by, enabled_at)
  on public.org_workflows to authenticated;
