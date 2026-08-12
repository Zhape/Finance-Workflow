"""Schema and engine.

One schema, two dialects. `FW_DATABASE_URL` unset means SQLite in the worker
directory, so tests and local runs need no external service; set it to the
Supabase Postgres URL and the same code runs there.

The Postgres schema is owned by the SQL migrations, not by this file --
`init_db()` only creates tables on SQLite. This table metadata exists so
queries are written once and run on both. It must stay in step with
`migrations/`; `test_schema_parity.py` checks that it does.

Tenancy: every org-owned table carries org_id and every query in `stores.py`
filters on it. The worker connects as the service role, which bypasses RLS,
so that filtering is the primary control -- RLS is the second line of defence
for anything holding a publishable key.
"""

from __future__ import annotations

import os

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
)

# Identifiers are `Uuid`, not a char column. Postgres has a real uuid type and
# rejects a varchar bind for it; SQLite has neither and stores char. Declaring
# the intent lets SQLAlchemy pick per dialect. `as_uuid=False` keeps the Python
# side as plain strings, which is what the API and JSON payloads want.
_Id = Uuid(as_uuid=False)

# `role` is a real enum type in Postgres (created by migration 0001), so a
# varchar bind is rejected. Declared this way SQLAlchemy binds the enum on
# Postgres and falls back to varchar-with-check on SQLite. The name must match
# the type in the migration.
_Role = Enum("admin", "member", "viewer", name="org_role", create_type=False)

metadata = MetaData()

orgs = Table(
    "orgs", metadata,
    Column("id", _Id, primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

org_members = Table(
    "org_members", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("user_id", _Id, primary_key=True),
    Column("email", Text, nullable=False),
    # admin: may connect integrations. member: may run and approve.
    # viewer: read only. Enforced in auth.py.
    Column("role", _Role, nullable=False, server_default="member"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

connections = Table(
    "connections", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("provider", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("label", Text),
    Column("tenant_id", Text),
    Column("tenant_name", Text),
    Column("secret", LargeBinary, nullable=False),
    Column("key_id", Text, nullable=False),
    # Which provider_apps row issued this token. A refresh token is bound to
    # the client that minted it, so using another app's credentials fails with
    # invalid_grant. Null means the platform's shared application.
    Column("app_name", Text),
    Column("connected_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("org_id", "provider", "name", name="connections_org_id_provider_name_key"),
)

# A per-org OAuth application. Overrides the platform default from the
# environment, so an org can bring its own Xero registration.
provider_apps = Table(
    "provider_apps", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("provider", Text, primary_key=True),
    # Several apps per provider: an unpublished Xero app may be connected to
    # only two organisations, so a customer with three needs more than one.
    Column("name", Text, primary_key=True, server_default="default"),
    Column("client_id", Text, nullable=False),
    Column("secret", LargeBinary, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("label", Text),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

oauth_states = Table(
    "oauth_states", metadata,
    Column("state", String(64), primary_key=True),
    Column("org_id", _Id, nullable=False),
    Column("provider", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("verifier", Text, nullable=False),
    Column("created_by", Text),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

vendor_bank_details = Table(
    "vendor_bank_details", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("region", Text, nullable=False),
    Column("vendor", Text, nullable=False),
    Column("vendor_key", Text, nullable=False),
    Column("fields", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("org_id", "region", "vendor_key",
                     name="vendor_bank_details_org_id_region_vendor_key_key"),
)

bank_layouts = Table(
    "bank_layouts", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("region", Text, primary_key=True),
    Column("columns", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# Which workflows an org has. Presence means enabled; absence means the org
# does not see the tile and cannot start a run. No implicit default -- a new
# org gets nothing until someone grants it.
org_workflows = Table(
    "org_workflows", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("workflow_key", Text, primary_key=True),
    Column("enabled_by", Text),
    Column("enabled_at", DateTime(timezone=True), server_default=func.now()),
)

# Per-org message templates. Absence of a row means "use the workflow's
# default", so unedited orgs keep receiving improvements to the shipped wording.
workflow_templates = Table(
    "workflow_templates", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("workflow_key", Text, primary_key=True),
    Column("variant", Text, primary_key=True),
    Column("subject", Text, nullable=False, server_default=""),
    Column("body", Text, nullable=False, server_default=""),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# A user's request for a workflow that does not exist yet. The worker turns
# these into pull requests; code review and merge stay the trust boundary.
workflow_requests = Table(
    "workflow_requests", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="submitted"),
    Column("kind", Text),
    Column("pr_url", Text),
    Column("error", Text),
    Column("requested_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

runs = Table(
    "runs", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("workflow", Text, nullable=False),
    Column("params", JSON, nullable=False),
    Column("status", Text, nullable=False),
    Column("summary", Text, server_default=""),
    Column("error", Text),
    Column("columns", JSON),
    Column("rows", JSON),
    Column("warnings", JSON),
    Column("log", JSON),
    Column("approved_ids", JSON),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("approved_by", Text),
    Column("approved_at", DateTime(timezone=True)),
    Column("artifact_name", Text),
    Column("artifact", LargeBinary),
)

# ---------------------------------------------------------------------------
# Invoice Inbox
# ---------------------------------------------------------------------------
# Owned by migration 0007. Note what is absent: no auto-send flag (the reply is
# a Gmail draft and a person presses send) and no watch/subscription state
# (ingestion is a button, not a subscription). Both absences are the design.

inbox_mailboxes = Table(
    "inbox_mailboxes", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    # The connections row holding the encrypted token for this mailbox.
    Column("connection_name", Text, nullable=False),
    Column("address", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="ok"),
    Column("last_synced_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("created_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("org_id", "address",
                     name="inbox_mailboxes_org_id_address_key"),
)

inbox_settings = Table(
    "inbox_settings", metadata,
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           primary_key=True),
    Column("lookback_days", Integer, nullable=False, server_default="7"),
    Column("xero_connection", Text, nullable=False, server_default="default"),
    # Which Xero organisation to read. Null falls back to the tenant recorded
    # on the connection itself — which is a guess, because the consent callback
    # stores whichever organisation Xero listed first.
    Column("xero_tenant_id", Text),
    # Null means the platform default from the environment.
    Column("classifier_model", Text),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

inbox_categories = Table(
    "inbox_categories", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("key", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("is_system", Boolean, nullable=False, server_default="false"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("sort_order", Integer, nullable=False, server_default="100"),
    Column("created_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("org_id", "key", name="inbox_categories_org_id_key_key"),
)

inbox_emails = Table(
    "inbox_emails", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("mailbox_id", _Id,
           ForeignKey("inbox_mailboxes.id", ondelete="CASCADE"),
           nullable=False),
    Column("gmail_message_id", Text, nullable=False),
    Column("gmail_thread_id", Text, nullable=False),
    Column("rfc822_message_id", Text),
    Column("in_reply_to", Text),
    # Not "references": reserved word in SQL.
    Column("email_references", Text),
    Column("from_name", Text),
    Column("from_email", Text, nullable=False, server_default=""),
    Column("subject", Text, nullable=False, server_default=""),
    Column("body_text", Text, nullable=False, server_default=""),
    Column("body_html", Text, nullable=False, server_default=""),
    Column("body_stripped", Text, nullable=False, server_default=""),
    Column("snippet", Text, nullable=False, server_default=""),
    Column("has_attachments", Boolean, nullable=False, server_default="false"),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("state", Text, nullable=False, server_default="received"),
    Column("state_reason", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("mailbox_id", "gmail_message_id",
                     name="inbox_emails_mailbox_id_gmail_message_id_key"),
)

inbox_classifications = Table(
    "inbox_classifications", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("email_id", _Id, ForeignKey("inbox_emails.id", ondelete="CASCADE"),
           nullable=False),
    Column("category_key", Text, nullable=False),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("secondary_key", Text),
    Column("secondary_confidence", Float),
    Column("multi_intent", Boolean, nullable=False, server_default="false"),
    Column("language", Text),
    Column("extracted", JSON, nullable=False),
    Column("model_version", Text),
    Column("latency_ms", Integer),
    Column("source", Text, nullable=False, server_default="ai"),
    Column("created_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

inbox_lookups = Table(
    "inbox_lookups", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("email_id", _Id, ForeignKey("inbox_emails.id", ondelete="CASCADE"),
           nullable=False),
    Column("outcome", Text, nullable=False),
    Column("xero_tenant_id", Text),
    Column("invoice_id", Text),
    Column("invoice_number", Text),
    Column("contact_name", Text),
    Column("amount", Float),
    Column("currency", Text),
    Column("due_date", Text),
    Column("description", Text),
    Column("summary", Text),
    Column("outstanding_balance", Float),
    Column("invoice_status", Text),
    Column("candidates", JSON, nullable=False),
    Column("mismatches", JSON, nullable=False),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

inbox_drafts = Table(
    "inbox_drafts", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("email_id", _Id, ForeignKey("inbox_emails.id", ondelete="CASCADE"),
           nullable=False),
    Column("category_key", Text, nullable=False),
    Column("template_version", Integer),
    Column("subject", Text, nullable=False, server_default=""),
    Column("body", Text, nullable=False, server_default=""),
    Column("fields", JSON, nullable=False),
    Column("missing_slots", JSON, nullable=False),
    Column("blockers", JSON, nullable=False),
    Column("edited", Boolean, nullable=False, server_default="false"),
    Column("updated_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# One reply per email, enforced here rather than in the UI.
inbox_replies = Table(
    "inbox_replies", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("email_id", _Id, ForeignKey("inbox_emails.id", ondelete="CASCADE"),
           nullable=False),
    Column("gmail_draft_id", Text),
    Column("gmail_thread_id", Text),
    Column("mailbox_address", Text),
    Column("category_key", Text, nullable=False),
    Column("template_version", Integer),
    Column("subject", Text, nullable=False, server_default=""),
    Column("body", Text, nullable=False, server_default=""),
    Column("fields", JSON, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("actor", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint("email_id", name="inbox_replies_email_id_key"),
)

inbox_template_versions = Table(
    "inbox_template_versions", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("variant", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("subject", Text, nullable=False, server_default=""),
    Column("body", Text, nullable=False, server_default=""),
    Column("updated_by", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("org_id", "variant", "version",
                     name="inbox_template_versions_org_id_variant_version_key"),
)

inbox_errors = Table(
    "inbox_errors", metadata,
    Column("id", _Id, primary_key=True),
    Column("org_id", _Id, ForeignKey("orgs.id", ondelete="CASCADE"),
           nullable=False),
    Column("email_id", _Id, ForeignKey("inbox_emails.id", ondelete="CASCADE")),
    Column("stage", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("message", Text, nullable=False, server_default=""),
    Column("attempts", Integer, nullable=False, server_default="1"),
    Column("resolved", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


Index("ix_runs_org_created", runs.c.org_id, runs.c.created_at.desc())
Index("ix_vendor_lookup", vendor_bank_details.c.org_id,
      vendor_bank_details.c.region, vendor_bank_details.c.vendor_key)
Index("ix_inbox_emails_org_received", inbox_emails.c.org_id,
      inbox_emails.c.received_at.desc())
Index("ix_inbox_emails_thread", inbox_emails.c.org_id,
      inbox_emails.c.gmail_thread_id)
Index("ix_inbox_errors_open", inbox_errors.c.org_id, inbox_errors.c.resolved,
      inbox_errors.c.created_at.desc())


def database_url() -> str:
    url = os.environ.get("FW_DATABASE_URL", "").strip()
    if url:
        # Supabase hands out postgres:// URLs; SQLAlchemy wants postgresql://.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        # And a bare postgresql:// makes SQLAlchemy reach for psycopg2, which
        # is not what we install. Pin the psycopg 3 driver explicitly so the
        # connection string can be pasted in unmodified from Supabase.
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    from pathlib import Path

    return f"sqlite:///{Path(__file__).resolve().parents[1] / 'fw.db'}"


def is_sqlite(url: str | None = None) -> bool:
    return (url or database_url()).startswith("sqlite")


def create_db_engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):
        # Workflows run off the request thread in a pool.
        return create_engine(
            url, future=True, connect_args={"check_same_thread": False}
        )
    return create_engine(url, future=True, pool_pre_ping=True, pool_size=5)


def init_db(engine) -> None:
    """Create tables on SQLite only. Postgres schema belongs to migrations."""
    if engine.url.get_backend_name() == "sqlite":
        metadata.create_all(engine)
