-- Invoice Inbox — customer invoice email, triaged.
--
-- Apply to Supabase project zacgedjltfkiyfydghtp as migration `invoice_inbox`.
--
-- The platform's first non-batch capability: mail arrives on its own schedule
-- rather than from a launch form. Two deliberate ceilings are visible in this
-- schema and should stay visible:
--
--   * There is no `auto_send_enabled` column, because nothing here sends. The
--     reply is created as a Gmail *draft* in the customer's own thread, and a
--     person presses send in Gmail. Adding a send path means adding a column
--     here, which is exactly the kind of change that should be hard to make by
--     accident.
--
--   * There is no watch/subscription state, because ingestion is a button. A
--     manual sync needs no always-on host, no Pub/Sub topic and no daily watch
--     renewal job — the three things that would otherwise have to work
--     perfectly before a single email arrived.
--
-- Same tenancy rules as everything else: every table carries org_id, the
-- worker filters on it in code, and RLS is the second line of defence. No
-- insert/update/delete policies — all mutation goes through the worker API,
-- which is where the audit trail lives.

-- ---------------------------------------------------------------------------
-- Connected mailboxes and org settings
-- ---------------------------------------------------------------------------

-- One row per connected Gmail account. Several per org is the normal case: the
-- finance inbox is often a Google Group delivering into two or three real
-- mailboxes, and the review screen shows the union of all of them.
create table public.inbox_mailboxes (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references public.orgs(id) on delete cascade,
  -- Points at connections.name, where the encrypted token actually lives.
  connection_name  text not null,
  address          text not null,
  status           text not null default 'ok',
  last_synced_at   timestamptz,
  last_error       text,
  created_by       text,
  created_at       timestamptz not null default now(),
  unique (org_id, address)
);

create table public.inbox_settings (
  org_id           uuid primary key references public.orgs(id) on delete cascade,
  -- How far back a sync looks. Bounded because each sync is a foreground
  -- request: an unbounded first sync on a busy mailbox would time out.
  lookback_days    integer not null default 7,
  -- Which Xero connection is authoritative for lookups.
  xero_connection  text not null default 'default',
  updated_by       text,
  updated_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Classification buckets — data, not code
-- ---------------------------------------------------------------------------
-- The classifier prompt and its response schema are assembled from the enabled
-- rows here at call time. That is what lets an org add a bucket without a
-- deploy: insert a category, write its template, and the next classification
-- can return it.
create table public.inbox_categories (
  id           uuid primary key default gen_random_uuid(),
  org_id       uuid not null references public.orgs(id) on delete cascade,
  key          text not null,
  label        text not null,
  -- Written for the classifier, not for the UI: this text is what the model
  -- reads to decide whether an email belongs in this bucket.
  description  text not null default '',
  is_system    boolean not null default false,
  enabled      boolean not null default true,
  sort_order   integer not null default 100,
  created_by   text,
  created_at   timestamptz not null default now(),
  unique (org_id, key)
);

-- ---------------------------------------------------------------------------
-- Ingested mail
-- ---------------------------------------------------------------------------

create table public.inbox_emails (
  id                 uuid primary key default gen_random_uuid(),
  org_id             uuid not null references public.orgs(id) on delete cascade,
  mailbox_id         uuid not null references public.inbox_mailboxes(id) on delete cascade,
  gmail_message_id   text not null,
  gmail_thread_id    text not null,
  -- The RFC822 Message-ID, used to collapse the copies a Google Group delivers
  -- into several connected mailboxes, and to thread replies.
  rfc822_message_id  text,
  in_reply_to        text,
  -- Not named "references": that is a reserved word in SQL and would need
  -- quoting at every use site.
  email_references   text,
  from_name          text,
  from_email         text not null default '',
  subject            text not null default '',
  body_text          text not null default '',
  body_html          text not null default '',
  -- Quoted history removed. This, not body_text, is what the classifier reads:
  -- a 60-message thread otherwise pays to classify the same words every time.
  body_stripped      text not null default '',
  snippet            text not null default '',
  has_attachments    boolean not null default false,
  received_at        timestamptz not null,
  state              text not null default 'received',
  state_reason       text,
  created_at         timestamptz not null default now(),
  -- The idempotency guarantee. All three ingest paths insert through this.
  unique (mailbox_id, gmail_message_id),
  constraint inbox_emails_state_valid
    check (state in ('received', 'suppressed', 'needs_review', 'drafted',
                     'draft_failed', 'dismissed'))
);

create index on public.inbox_emails (org_id, received_at desc);
create index on public.inbox_emails (org_id, gmail_thread_id);

-- ---------------------------------------------------------------------------
-- What the AI said, what Xero said, what we drafted
-- ---------------------------------------------------------------------------

-- Append-only. A human overriding the category writes a second row with
-- source='human' rather than editing the first, so the model's original
-- suggestion survives next to the correction — which is the only way an
-- accuracy regression can be attributed to a model change later.
create table public.inbox_classifications (
  id                    uuid primary key default gen_random_uuid(),
  org_id                uuid not null references public.orgs(id) on delete cascade,
  email_id              uuid not null references public.inbox_emails(id) on delete cascade,
  category_key          text not null,
  confidence            double precision not null default 0,
  secondary_key         text,
  secondary_confidence  double precision,
  -- Set when a secondary category scores above the multi-intent threshold.
  -- "I've paid 1042 and I'm disputing 1043" must never be answered as if it
  -- were only the first half.
  multi_intent          boolean not null default false,
  language              text,
  extracted             jsonb not null default '{}'::jsonb,
  model_version         text,
  latency_ms            integer,
  source                text not null default 'ai',
  created_by            text,
  created_at            timestamptz not null default now(),
  constraint inbox_classifications_source_valid
    check (source in ('ai', 'human', 'fallback'))
);

create index on public.inbox_classifications (email_id, created_at desc);

-- Never cached: balance and status must be true at the moment we answer. A
-- credit note or part payment means outstanding_balance <> amount, and the
-- templates inject the balance.
create table public.inbox_lookups (
  id                   uuid primary key default gen_random_uuid(),
  org_id               uuid not null references public.orgs(id) on delete cascade,
  email_id             uuid not null references public.inbox_emails(id) on delete cascade,
  outcome              text not null,
  xero_tenant_id       text,
  invoice_id           text,
  invoice_number       text,
  contact_name         text,
  amount               double precision,
  currency             text,
  due_date             text,
  description          text,
  -- The line items condensed, for the "what is this invoice for" reply. Built
  -- in the application layer from Xero's own words — no model involved.
  summary              text,
  outstanding_balance  double precision,
  invoice_status       text,
  -- Open invoices for this contact, when the number did not resolve. This is
  -- what lets a manager pick the right one after a customer quoted their own
  -- PO number.
  candidates           jsonb not null default '[]'::jsonb,
  -- Fields where the customer's claim differs from Xero, both values kept.
  mismatches           jsonb not null default '[]'::jsonb,
  error                text,
  created_at           timestamptz not null default now(),
  constraint inbox_lookups_outcome_valid
    check (outcome in ('found', 'not_found', 'ambiguous', 'timed_out',
                       'skipped', 'error'))
);

create index on public.inbox_lookups (email_id, created_at desc);

-- The current draft for an email. Superseded rather than updated when the
-- category changes, so the trail of what was offered stays intact.
create table public.inbox_drafts (
  id                uuid primary key default gen_random_uuid(),
  org_id            uuid not null references public.orgs(id) on delete cascade,
  email_id          uuid not null references public.inbox_emails(id) on delete cascade,
  category_key      text not null,
  template_version  integer,
  subject           text not null default '',
  body              text not null default '',
  fields            jsonb not null default '{}'::jsonb,
  -- Placeholders with nothing to put in them. Non-empty blocks drafting, in
  -- the UI and again in the API.
  missing_slots     jsonb not null default '[]'::jsonb,
  blockers          jsonb not null default '[]'::jsonb,
  edited            boolean not null default false,
  updated_by        text,
  created_at        timestamptz not null default now()
);

create index on public.inbox_drafts (email_id, created_at desc);

-- ---------------------------------------------------------------------------
-- The reply, as a Gmail draft
-- ---------------------------------------------------------------------------
-- One row per email, enforced by the database rather than by UI state: two
-- browser tabs, a double-click and a replayed API call all lose to the same
-- unique constraint. The body and field map are stored as at draft time and
-- never re-rendered against live Xero, or the record would start lying about
-- what the customer was told.
create table public.inbox_replies (
  id                uuid primary key default gen_random_uuid(),
  org_id            uuid not null references public.orgs(id) on delete cascade,
  email_id          uuid not null references public.inbox_emails(id) on delete cascade,
  gmail_draft_id    text,
  gmail_thread_id   text,
  mailbox_address   text,
  category_key      text not null,
  template_version  integer,
  subject           text not null default '',
  body              text not null default '',
  fields            jsonb not null default '{}'::jsonb,
  status            text not null default 'pending',
  attempts          integer not null default 0,
  error             text,
  actor             text not null,
  created_at        timestamptz not null default now(),
  completed_at      timestamptz,
  unique (email_id),
  constraint inbox_replies_status_valid
    check (status in ('pending', 'created', 'failed'))
);

-- Append-only wording history. inbox_replies.template_version points here, so
-- an audit row can be read back against the exact words that were approved.
create table public.inbox_template_versions (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.orgs(id) on delete cascade,
  variant     text not null,
  version     integer not null,
  subject     text not null default '',
  body        text not null default '',
  updated_by  text,
  created_at  timestamptz not null default now(),
  unique (org_id, variant, version)
);

-- Every failure that a person might need to act on. Surfaced as a banner on
-- the inbox screen rather than left in a log nobody opens.
create table public.inbox_errors (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.orgs(id) on delete cascade,
  email_id    uuid references public.inbox_emails(id) on delete cascade,
  stage       text not null,
  code        text not null,
  message     text not null default '',
  attempts    integer not null default 1,
  resolved    boolean not null default false,
  created_at  timestamptz not null default now()
);

create index on public.inbox_errors (org_id, resolved, created_at desc);

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table public.inbox_mailboxes         enable row level security;
alter table public.inbox_settings          enable row level security;
alter table public.inbox_categories        enable row level security;
alter table public.inbox_emails            enable row level security;
alter table public.inbox_classifications   enable row level security;
alter table public.inbox_lookups           enable row level security;
alter table public.inbox_drafts            enable row level security;
alter table public.inbox_replies           enable row level security;
alter table public.inbox_template_versions enable row level security;
alter table public.inbox_errors            enable row level security;

create policy inbox_mailboxes_read on public.inbox_mailboxes
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_settings_read on public.inbox_settings
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_categories_read on public.inbox_categories
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_emails_read on public.inbox_emails
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_classifications_read on public.inbox_classifications
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_lookups_read on public.inbox_lookups
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_drafts_read on public.inbox_drafts
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_replies_read on public.inbox_replies
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_template_versions_read on public.inbox_template_versions
  for select to authenticated using (private.is_org_member(org_id));
create policy inbox_errors_read on public.inbox_errors
  for select to authenticated using (private.is_org_member(org_id));
