"""Org-scoped data access.

Every method takes an org_id and every query filters on it. Nothing in this
module can be called in a way that spans orgs -- that is deliberate, and it is
why handlers never write SQL themselves.

The worker connects as the Supabase service role, which bypasses RLS. So the
`where org_id == ...` clauses here are the primary tenancy control, not a
convenience. Treat a missing one as a security bug.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update

from .banking import norm_header, norm_vendor, normalise_account
from .crypto import Cipher
from .db import (
    org_workflows,
    workflow_requests,
    workflow_templates,
    provider_apps,
    bank_layouts,
    connections,
    oauth_states,
    org_members,
    orgs,
    runs,
    vendor_bank_details,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Orgs
# ---------------------------------------------------------------------------

class OrgStore:
    def __init__(self, engine):
        self._engine = engine

    def create(self, name: str, owner_user_id: str, owner_email: str) -> str:
        org_id = _uuid()
        with self._engine.begin() as conn:
            conn.execute(insert(orgs).values(id=org_id, name=name))
            conn.execute(insert(org_members).values(
                org_id=org_id, user_id=owner_user_id,
                email=owner_email, role="admin",
            ))
        return org_id

    def members(self, org_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(org_members.c.user_id, org_members.c.email,
                       org_members.c.role)
                .where(org_members.c.org_id == org_id)
                .order_by(org_members.c.created_at)
            ).all()
        return [
            {"userId": str(r.user_id), "email": r.email, "role": str(r.role)}
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Connections — implements the TokenStore protocol in xero.py
# ---------------------------------------------------------------------------

class ConnectionStore:
    """Encrypted OAuth tokens, one row per (org, provider, connection name)."""

    def __init__(self, engine, cipher: Cipher | None = None,
                 provider: str = "xero"):
        self._engine = engine
        self._cipher = cipher or Cipher()
        self._provider = provider

    def load(self, org_id: str, connection: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(connections.c.secret, connections.c.tenant_id,
                       connections.c.tenant_name)
                .where(connections.c.org_id == org_id)
                .where(connections.c.provider == self._provider)
                .where(connections.c.name == connection)
            ).first()
        if row is None:
            return None
        token = json.loads(self._cipher.decrypt(row.secret))
        # tenant_id lives in its own column so it can be shown in the UI
        # without decrypting anything.
        token.setdefault("tenant_id", row.tenant_id)
        token.setdefault("tenant_name", row.tenant_name)
        return token

    def save(self, org_id: str, connection: str, token: dict[str, Any]) -> None:
        secret = self._cipher.encrypt(json.dumps(token))
        values = {
            "secret": secret,
            "key_id": self._cipher.key_id,
            "tenant_id": token.get("tenant_id"),
            "tenant_name": token.get("tenant_name"),
            "updated_at": _now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(connections.c.id)
                .where(connections.c.org_id == org_id)
                .where(connections.c.provider == self._provider)
                .where(connections.c.name == connection)
            ).first()
            if existing:
                conn.execute(
                    update(connections)
                    .where(connections.c.id == existing.id)
                    .values(**values)
                )
            else:
                conn.execute(insert(connections).values(
                    id=_uuid(), org_id=org_id, provider=self._provider,
                    name=connection, label=connection.upper(),
                    connected_by=token.get("connected_by"), **values,
                ))

    def list(self, org_id: str,
             provider: str | None = None) -> list[dict[str, Any]]:
        """Connection metadata for the UI. Never returns the secret.

        `provider` is optional because the settings screen wants every
        connection at once. Callers that mean "this org's Xero connections"
        must pass it: connection names are only unique per provider, so a
        Google connection called 'default' and a Xero one called 'default' are
        different rows that an unfiltered list makes indistinguishable.
        """
        stmt = (select(connections.c.name, connections.c.label,
                       connections.c.provider, connections.c.tenant_name,
                       connections.c.connected_by, connections.c.updated_at)
                .where(connections.c.org_id == org_id)
                .order_by(connections.c.name))
        if provider:
            stmt = stmt.where(connections.c.provider == provider)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            {
                "name": r.name,
                "label": r.label,
                "provider": r.provider,
                "tenantName": r.tenant_name,
                "connectedBy": r.connected_by,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    def disconnect(self, org_id: str, connection: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(connections)
                .where(connections.c.org_id == org_id)
                .where(connections.c.provider == self._provider)
                .where(connections.c.name == connection)
            )
        return result.rowcount > 0


class TemplateStore:
    """Per-org message templates, falling back to the workflow's defaults.

    Absence of a row means "use the default". That is why `get` merges rather
    than seeding rows at org creation: an org that never edits the wording
    keeps receiving improvements to it, while an org that has edited keeps its
    own words even when the shipped default changes.
    """

    def __init__(self, engine):
        self._engine = engine

    def overrides(self, org_id: str, workflow_key: str) -> dict[str, dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(workflow_templates.c.variant,
                       workflow_templates.c.subject,
                       workflow_templates.c.body,
                       workflow_templates.c.updated_by,
                       workflow_templates.c.updated_at)
                .where(workflow_templates.c.org_id == org_id)
                .where(workflow_templates.c.workflow_key == workflow_key)
            ).all()
        return {
            r.variant: {
                "subject": r.subject,
                "body": r.body,
                "updatedBy": r.updated_by,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        }

    def merged(self, org_id: str, workflow_key: str,
               defaults: dict[str, dict]) -> dict[str, dict]:
        """Defaults with this org's overrides applied, marked so the UI can
        show which are customised."""
        custom = self.overrides(org_id, workflow_key)
        out: dict[str, dict] = {}
        for variant, default in defaults.items():
            override = custom.get(variant)
            out[variant] = {
                "subject": (override or default)["subject"],
                "body": (override or default)["body"],
                "customised": override is not None,
                "default": default,
                "updatedBy": (override or {}).get("updatedBy"),
                "updatedAt": (override or {}).get("updatedAt"),
            }
        return out

    def save(self, org_id: str, workflow_key: str, variant: str,
             subject: str, body: str, user: str) -> None:
        values = {"subject": subject, "body": body,
                  "updated_by": user, "updated_at": _now()}
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(workflow_templates.c.variant)
                .where(workflow_templates.c.org_id == org_id)
                .where(workflow_templates.c.workflow_key == workflow_key)
                .where(workflow_templates.c.variant == variant)
            ).first()
            if existing:
                conn.execute(
                    update(workflow_templates)
                    .where(workflow_templates.c.org_id == org_id)
                    .where(workflow_templates.c.workflow_key == workflow_key)
                    .where(workflow_templates.c.variant == variant)
                    .values(**values)
                )
            else:
                conn.execute(insert(workflow_templates).values(
                    org_id=org_id, workflow_key=workflow_key,
                    variant=variant, **values
                ))

    def reset(self, org_id: str, workflow_key: str,
              variant: str | None = None) -> int:
        """Drop overrides, reverting to the shipped wording."""
        stmt = (delete(workflow_templates)
                .where(workflow_templates.c.org_id == org_id)
                .where(workflow_templates.c.workflow_key == workflow_key))
        if variant:
            stmt = stmt.where(workflow_templates.c.variant == variant)
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount


class WorkflowRequestStore:
    """Requests for workflows that do not exist yet, and what became of them."""

    def __init__(self, engine):
        self._engine = engine

    def create(self, org_id: str, title: str, description: str,
               user: str) -> str:
        request_id = _uuid()
        with self._engine.begin() as conn:
            conn.execute(insert(workflow_requests).values(
                id=request_id, org_id=org_id, title=title,
                description=description, status="submitted",
                requested_by=user, created_at=_now(),
            ))
        return request_id

    def resolve(self, org_id: str, request_id: str, *, pr_url: str | None,
                kind: str | None, error: str | None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(workflow_requests)
                .where(workflow_requests.c.id == request_id)
                .where(workflow_requests.c.org_id == org_id)
                .values(
                    status="failed" if error else "pr_opened",
                    pr_url=pr_url, kind=kind, error=error,
                )
            )

    def list(self, org_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(workflow_requests)
                .where(workflow_requests.c.org_id == org_id)
                .order_by(workflow_requests.c.created_at.desc())
                .limit(limit)
            ).all()
        return [
            {
                "id": str(r.id),
                "title": r.title,
                "description": r.description,
                "status": r.status,
                "kind": r.kind,
                "prUrl": r.pr_url,
                "error": r.error,
                "requestedBy": r.requested_by,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


class WorkflowAccessStore:
    """Which workflows an org has.

    Fails closed: an org with no rows has no workflows. That is why `grant` is
    called explicitly at org creation rather than defaulting to everything --
    the expensive mistake here is an org seeing a workflow that reads a system
    it has no business reading.
    """

    def __init__(self, engine):
        self._engine = engine

    def keys(self, org_id: str) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(org_workflows.c.workflow_key)
                .where(org_workflows.c.org_id == org_id)
            ).scalars().all()
        return set(rows)

    def has(self, org_id: str, workflow_key: str) -> bool:
        return workflow_key in self.keys(org_id)

    def grant(self, org_id: str, workflow_key: str, user: str | None = None) -> None:
        if self.has(org_id, workflow_key):
            return
        with self._engine.begin() as conn:
            conn.execute(insert(org_workflows).values(
                org_id=org_id, workflow_key=workflow_key,
                enabled_by=user, enabled_at=_now(),
            ))

    def revoke(self, org_id: str, workflow_key: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(org_workflows)
                .where(org_workflows.c.org_id == org_id)
                .where(org_workflows.c.workflow_key == workflow_key)
            )
        return result.rowcount > 0


class ProviderAppStore:
    """The OAuth application an org connects through.

    Resolution is org row first, platform environment second. That ordering is
    the whole design: most orgs never register an app and ride the platform's,
    while an org that insists on its own gets it without a code change.
    """

    def __init__(self, engine, cipher: Cipher | None = None,
                 provider: str = "xero"):
        self._engine = engine
        self._cipher = cipher or Cipher()
        self._provider = provider

    def get(self, org_id: str) -> tuple[str, str] | None:
        """Return (client_id, client_secret) for this org, or None to fall back."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(provider_apps.c.client_id, provider_apps.c.secret)
                .where(provider_apps.c.org_id == org_id)
                .where(provider_apps.c.provider == self._provider)
            ).first()
        if row is None:
            return None
        return row.client_id, self._cipher.decrypt(row.secret)

    def status(self, org_id: str) -> dict[str, Any]:
        """Metadata for the UI. Returns the client id, never the secret."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(provider_apps.c.client_id, provider_apps.c.label,
                       provider_apps.c.updated_by, provider_apps.c.updated_at)
                .where(provider_apps.c.org_id == org_id)
                .where(provider_apps.c.provider == self._provider)
            ).first()
        if row is None:
            return {"source": "platform", "clientId": None}
        return {
            "source": "org",
            "clientId": row.client_id,
            "label": row.label,
            "updatedBy": row.updated_by,
            "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        }

    def save(self, org_id: str, client_id: str, client_secret: str,
             user: str, label: str | None = None) -> None:
        values = {
            "client_id": client_id.strip(),
            "secret": self._cipher.encrypt(client_secret),
            "key_id": self._cipher.key_id,
            "label": label,
            "updated_by": user,
            "updated_at": _now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(provider_apps.c.org_id)
                .where(provider_apps.c.org_id == org_id)
                .where(provider_apps.c.provider == self._provider)
            ).first()
            if existing:
                conn.execute(
                    update(provider_apps)
                    .where(provider_apps.c.org_id == org_id)
                    .where(provider_apps.c.provider == self._provider)
                    .values(**values)
                )
            else:
                conn.execute(insert(provider_apps).values(
                    org_id=org_id, provider=self._provider, **values
                ))

    def clear(self, org_id: str) -> bool:
        """Revert this org to the platform application."""
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(provider_apps)
                .where(provider_apps.c.org_id == org_id)
                .where(provider_apps.c.provider == self._provider)
            )
        return result.rowcount > 0


class OAuthStateStore:
    """Short-lived PKCE state. The verifier never reaches the browser."""

    TTL = timedelta(minutes=10)

    def __init__(self, engine):
        self._engine = engine

    def put(self, state: str, org_id: str, provider: str, name: str,
            verifier: str, created_by: str) -> None:
        with self._engine.begin() as conn:
            # Opportunistic sweep — no scheduler needed for a table this small.
            conn.execute(delete(oauth_states)
                         .where(oauth_states.c.expires_at < _now()))
            conn.execute(insert(oauth_states).values(
                state=state, org_id=org_id, provider=provider, name=name,
                verifier=verifier, created_by=created_by,
                expires_at=_now() + self.TTL,
            ))

    def take(self, state: str) -> dict[str, Any] | None:
        """Consume a state exactly once; returns None if unknown or expired."""
        with self._engine.begin() as conn:
            row = conn.execute(
                select(oauth_states).where(oauth_states.c.state == state)
            ).first()
            if row is None:
                return None
            conn.execute(delete(oauth_states).where(oauth_states.c.state == state))

        expires = row.expires_at
        if expires.tzinfo is None:          # SQLite returns naive datetimes
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < _now():
            return None
        return {
            "org_id": str(row.org_id), "provider": row.provider,
            "name": row.name, "verifier": row.verifier,
            "created_by": row.created_by,
        }


# ---------------------------------------------------------------------------
# Vendor bank details — implements BankDetailsSource
# ---------------------------------------------------------------------------

class VendorStore:
    """Per-org vendor banking data, replacing the mapping sheet."""

    def __init__(self, engine, org_id: str, region: str):
        self._engine = engine
        self._org_id = org_id
        self._region = region
        self._cache: dict[str, dict[str, str]] | None = None
        self._keys: list[str] = []

    def _load(self) -> dict[str, dict[str, str]]:
        if self._cache is None:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(vendor_bank_details.c.vendor_key,
                           vendor_bank_details.c.fields)
                    .where(vendor_bank_details.c.org_id == self._org_id)
                    .where(vendor_bank_details.c.region == self._region)
                ).all()
            cache: dict[str, dict[str, str]] = {}
            keys: list[str] = []
            for r in rows:
                fields = r.fields if isinstance(r.fields, dict) else json.loads(r.fields)
                cache[r.vendor_key] = fields
                for k in fields:
                    if k not in keys:
                        keys.append(k)
            self._cache = cache
            self._keys = keys
        return self._cache

    def field_keys(self) -> list[str]:
        self._load()
        return list(self._keys)

    def lookup(self, vendor_name: str) -> dict[str, str]:
        fields = self._load().get(norm_vendor(vendor_name))
        if fields is None:
            return {}
        return {k: fields.get(k, "") for k in self._keys}

    @staticmethod
    def replace_region(engine, org_id: str, region: str,
                       records: list[dict[str, Any]],
                       vendor_key: str = "vendor") -> int:
        """Replace an org's vendor list for a region (onboarding import).

        Whole-region replace rather than upsert: a vendor removed from the
        source must disappear here too, or a stale account number outlives
        the change that was meant to remove it.
        """
        vkey = norm_header(vendor_key)
        rows = []
        for rec in records:
            normalised = {norm_header(k): v for k, v in rec.items()}
            vendor = normalised.pop(vkey, None)
            if not vendor:
                continue
            fields = {
                k: (normalise_account(v) if k == "accountnumber" else
                    ("" if v is None else str(v).strip()))
                for k, v in normalised.items()
            }
            rows.append({
                "id": _uuid(), "org_id": org_id, "region": region,
                "vendor": str(vendor).strip(),
                "vendor_key": norm_vendor(vendor),
                "fields": fields, "updated_at": _now(),
            })

        with engine.begin() as conn:
            conn.execute(
                delete(vendor_bank_details)
                .where(vendor_bank_details.c.org_id == org_id)
                .where(vendor_bank_details.c.region == region)
            )
            if rows:
                conn.execute(insert(vendor_bank_details), rows)
        return len(rows)


class LayoutStore:
    def __init__(self, engine):
        self._engine = engine

    def get(self, org_id: str, region: str) -> list[str] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(bank_layouts.c.columns)
                .where(bank_layouts.c.org_id == org_id)
                .where(bank_layouts.c.region == region)
            ).first()
        if row is None:
            return None
        return row.columns if isinstance(row.columns, list) else json.loads(row.columns)

    def set(self, org_id: str, region: str, columns: list[str]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                delete(bank_layouts)
                .where(bank_layouts.c.org_id == org_id)
                .where(bank_layouts.c.region == region)
            )
            conn.execute(insert(bank_layouts).values(
                org_id=org_id, region=region, columns=columns,
                updated_at=_now(),
            ))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class RunStore:
    def __init__(self, engine):
        self._engine = engine

    def create(self, org_id: str, workflow: str, params: dict, user: str) -> str:
        run_id = _uuid()
        with self._engine.begin() as conn:
            conn.execute(insert(runs).values(
                id=run_id, org_id=org_id, workflow=workflow, params=params,
                status="needs_approval", created_by=user, created_at=_now(),
                columns=[], rows=[], warnings=[], log=[], approved_ids=[],
            ))
        return run_id

    def finish_pull(self, org_id: str, run_id: str, result) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id).where(runs.c.org_id == org_id)
                .values(
                    status=result.status.value,
                    columns=[{"key": c.key, "label": c.label, "type": c.type}
                             for c in result.columns],
                    rows=result.rows,
                    warnings=result.warnings,
                    log=result.log,
                    summary=result.summary,
                )
            )

    def fail(self, org_id: str, run_id: str, error: str, log: list[str]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id).where(runs.c.org_id == org_id)
                .values(status="failed", error=error, log=log)
            )

    def complete(self, org_id: str, run_id: str, result,
                 approved_ids: list[str], user: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id).where(runs.c.org_id == org_id)
                .values(
                    status=result.status.value,
                    summary=result.summary,
                    approved_ids=approved_ids,
                    approved_by=user,
                    approved_at=_now(),
                    artifact_name=result.artifact_name,
                    artifact=result.artifact_bytes,
                )
            )

    def get(self, org_id: str, run_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(runs)
                .where(runs.c.id == run_id)
                .where(runs.c.org_id == org_id)
            ).first()
        return _run_json(row) if row else None

    def artifact(self, org_id: str, run_id: str) -> tuple[str, bytes] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(runs.c.artifact_name, runs.c.artifact)
                .where(runs.c.id == run_id)
                .where(runs.c.org_id == org_id)
            ).first()
        if row is None or row.artifact is None:
            return None
        return row.artifact_name, bytes(row.artifact)

    def list(self, org_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(runs)
                .where(runs.c.org_id == org_id)
                .order_by(runs.c.created_at.desc())
                .limit(limit)
            ).all()
        return [_run_json(r, include_rows=False) for r in rows]


def _coerce(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _run_json(row, include_rows: bool = True) -> dict[str, Any]:
    data = {
        "id": str(row.id),
        "workflow": row.workflow,
        "params": _coerce(row.params, {}),
        "status": row.status,
        "summary": row.summary or "",
        "warnings": _coerce(row.warnings, []),
        "log": _coerce(row.log, []),
        "error": row.error,
        "createdBy": row.created_by,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "approvedBy": row.approved_by,
        "approvedAt": row.approved_at.isoformat() if row.approved_at else None,
        "artifactName": row.artifact_name,
    }
    rows = _coerce(row.rows, [])
    data["rowCount"] = len(rows)
    if include_rows:
        data["columns"] = _coerce(row.columns, [])
        data["rows"] = rows
    return data
