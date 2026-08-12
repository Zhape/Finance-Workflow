-- The membership helpers are SECURITY DEFINER (they must be, or the policy on
-- org_members recurses into itself). Sitting in `public` also made them
-- callable as RPC endpoints by anon -- flagged by the Supabase security
-- advisor as anon_security_definer_function_executable. Moving them to a
-- schema PostgREST does not expose keeps the RLS behaviour and removes the
-- endpoint.
--
-- Applied to Supabase project zacgedjltfkiyfydghtp as migration
-- `harden_membership_helpers`.

create schema if not exists private;
revoke all on schema private from anon, authenticated;
grant usage on schema private to authenticated, service_role;

create function private.is_org_member(target_org uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.org_members m
    where m.org_id = target_org and m.user_id = auth.uid()
  );
$$;

create function private.has_org_role(target_org uuid, roles public.org_role[])
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.org_members m
    where m.org_id = target_org
      and m.user_id = auth.uid()
      and m.role = any(roles)
  );
$$;

revoke all on function private.is_org_member(uuid) from public, anon;
revoke all on function private.has_org_role(uuid, public.org_role[]) from public, anon;
grant execute on function private.is_org_member(uuid) to authenticated, service_role;
grant execute on function private.has_org_role(uuid, public.org_role[])
  to authenticated, service_role;

create policy org_read on public.orgs
  for select to authenticated using (private.is_org_member(id));

create policy member_read on public.org_members
  for select to authenticated using (private.is_org_member(org_id));

create policy connection_read on public.connections
  for select to authenticated using (private.is_org_member(org_id));

create policy vendor_read on public.vendor_bank_details
  for select to authenticated using (private.is_org_member(org_id));

create policy layout_read on public.bank_layouts
  for select to authenticated using (private.is_org_member(org_id));

create policy run_read on public.runs
  for select to authenticated using (private.is_org_member(org_id));

-- Explicit deny, so "no policy" reads as a decision rather than an oversight.
create policy oauth_state_no_client_access on public.oauth_states
  for select to authenticated using (false);
