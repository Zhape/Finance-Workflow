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
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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

Index("ix_runs_org_created", runs.c.org_id, runs.c.created_at.desc())
Index("ix_vendor_lookup", vendor_bank_details.c.org_id,
      vendor_bank_details.c.region, vendor_bank_details.c.vendor_key)


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
