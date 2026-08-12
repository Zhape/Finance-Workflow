-- Several OAuth applications per organisation, and a connection per Xero org.
--
-- Apply to Supabase project zacgedjltfkiyfydghtp as migration
-- `multiple_provider_apps`.
--
-- Two constraints forced this, both from Xero rather than from taste.
--
--   * An unpublished Xero app may be connected to at most two organisations.
--     A customer with three therefore needs two apps, so "the org's own app"
--     could not stay singular. `provider_apps` gains a name and the primary
--     key widens to include it.
--
--   * A refresh token is bound to the client that issued it. With more than
--     one app in play, a connection must record which one created it or the
--     refresh silently uses the wrong client and fails with invalid_grant
--     mid-run. `connections.app_name` is that record; null means the
--     platform's shared application.
--
-- Connections also stop being fixed slots. The consent callback previously
-- stored `found[0]` — whichever organisation Xero happened to list first —
-- against a hardcoded name like 'us', so two differently named connections
-- could point at the same company and the screen could not tell you. One row
-- per granted organisation, named from Xero, removes the guess entirely.

-- ---------------------------------------------------------------------------
-- provider_apps: several per (org, provider)
-- ---------------------------------------------------------------------------

alter table public.provider_apps
  add column name text not null default 'default';

-- `label` already exists and stays free text for a human ("UK & EU app").
-- `name` is the stable identifier the consent flow passes around.
alter table public.provider_apps drop constraint provider_apps_pkey;
alter table public.provider_apps
  add constraint provider_apps_pkey primary key (org_id, provider, name);

-- ---------------------------------------------------------------------------
-- connections: remember which application issued the token
-- ---------------------------------------------------------------------------

alter table public.connections
  add column app_name text;

-- Existing connections were issued by the org's single app where one exists,
-- and by the platform app otherwise. Backfilled rather than left null so a
-- refresh after this migration resolves the same client that issued the token.
update public.connections c
   set app_name = 'default'
 where exists (
   select 1 from public.provider_apps p
    where p.org_id = c.org_id and p.provider = c.provider
 );
