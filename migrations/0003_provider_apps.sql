-- Per-org OAuth application credentials, editable from Settings.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration
-- `provider_apps_per_org`.
--
-- Each org may register its own Xero app. A row here overrides the platform
-- default (the FW_XERO_* environment variables), so an org that wants its own
-- Xero registration -- its own consent screen, its own revocation, its own
-- rate limits -- can have one, while an org that does not need never see this.
--
-- The client secret is encrypted with the same Fernet key as connections.secret
-- and, like it, is unreadable by a browser session. The client id IS readable:
-- the UI must show which app is in use, and a client id is not a secret.

create table public.provider_apps (
  org_id      uuid not null references public.orgs(id) on delete cascade,
  provider    text not null,
  client_id   text not null,
  secret      bytea not null,
  key_id      text not null,
  label       text,
  updated_by  text,
  updated_at  timestamptz not null default now(),
  primary key (org_id, provider)
);

alter table public.provider_apps enable row level security;

create policy provider_app_read on public.provider_apps
  for select to authenticated using (private.is_org_member(org_id));

-- Rows are written by the worker only, as with every other table. Column
-- grants keep the secret unreadable even for a member of the owning org.
revoke all on public.provider_apps from anon, authenticated;
grant select (org_id, provider, client_id, label, updated_by, updated_at)
  on public.provider_apps to authenticated;
