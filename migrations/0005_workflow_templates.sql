-- Per-org message templates for a workflow.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration
-- `workflow_templates`.
--
-- A row overrides the default that ships with the workflow. Absence means "use
-- the default", so an org that never edits anything keeps getting improvements
-- to the shipped wording, and an org that has edited keeps its own words even
-- when the default changes. Storing a copy of the default for everyone would
-- silently freeze every org at whatever the wording was on the day they signed
-- up.
--
-- `variant` is the workflow's own key for the template -- for the chase-up it
-- is the tone: gentle, reminder, firm, final.
create table public.workflow_templates (
  org_id       uuid not null references public.orgs(id) on delete cascade,
  workflow_key text not null,
  variant      text not null,
  subject      text not null default '',
  body         text not null default '',
  updated_by   text,
  updated_at   timestamptz not null default now(),
  primary key (org_id, workflow_key, variant)
);

alter table public.workflow_templates enable row level security;

create policy workflow_template_read on public.workflow_templates
  for select to authenticated using (private.is_org_member(org_id));

revoke all on public.workflow_templates from anon, authenticated;
grant select (org_id, workflow_key, variant, subject, body, updated_by, updated_at)
  on public.workflow_templates to authenticated;
