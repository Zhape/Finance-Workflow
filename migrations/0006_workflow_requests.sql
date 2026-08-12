-- Workflow requests: a user describes the workflow they want; the worker
-- opens a pull request carrying the request (and, when generation is
-- configured, a drafted module and tests). Nothing here executes user-derived
-- code: the PR is a proposal, and code review plus merge remain the trust
-- boundary.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration
-- `workflow_requests`.
create table public.workflow_requests (
  id            uuid primary key default gen_random_uuid(),
  org_id        uuid not null references public.orgs(id) on delete cascade,
  title         text not null,
  description   text not null,
  status        text not null default 'submitted',
  kind          text,                 -- 'generated' | 'spec'
  pr_url        text,
  error         text,
  requested_by  text not null,
  created_at    timestamptz not null default now(),
  constraint workflow_requests_status_valid
    check (status in ('submitted', 'pr_opened', 'failed'))
);

create index on public.workflow_requests (org_id, created_at desc);

alter table public.workflow_requests enable row level security;

create policy workflow_request_read on public.workflow_requests
  for select to authenticated using (private.is_org_member(org_id));

revoke all on public.workflow_requests from anon, authenticated;
grant select (id, org_id, title, description, status, kind, pr_url,
              requested_by, created_at)
  on public.workflow_requests to authenticated;
