-- Let an org choose which Xero organisation and which model the inbox uses.
--
-- Apply to Supabase project zacgedjltfkiyfydghtp as migration `inbox_targets`.
--
-- Both columns exist because the previous defaults were guesses the user could
-- not correct:
--
--   * `xero_tenant_id` — one Xero token can reach several organisations, and
--     the consent callback stores whichever the API happened to list first.
--     That made two differently-named connections resolve to the same org with
--     no way to tell, let alone change. The inbox now records the organisation
--     it was told to read, and falls back to the connection's own tenant when
--     nothing is chosen.
--
--   * `classifier_model` — the model name lived only in the worker's
--     environment, so a name the org's API key cannot reach meant editing a
--     hosting dashboard and redeploying to try another. Null means "use the
--     platform default", the same rule the templates follow.

alter table public.inbox_settings
  add column xero_tenant_id   text,
  add column classifier_model text;
