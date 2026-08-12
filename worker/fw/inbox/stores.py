"""Org-scoped data access for the Invoice Inbox.

Same rule as `fw/stores.py`, for the same reason: the worker connects as the
service role and bypasses RLS, so the `where org_id ==` clause in every query
here is the primary tenancy control. A missing one is a security bug, not a
style issue.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from ..db import (
    inbox_categories,
    inbox_classifications,
    inbox_drafts,
    inbox_emails,
    inbox_errors,
    inbox_lookups,
    inbox_mailboxes,
    inbox_replies,
    inbox_settings,
    inbox_template_versions,
)
from .models import Classification, Extracted, State, SYSTEM_CATEGORIES, CategoryDef


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite hands back naive datetimes; treating those as local time would
    # drift every displayed age by the machine's offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class MailboxStore:
    def __init__(self, engine):
        self._engine = engine

    def list(self, org_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_mailboxes)
                .where(inbox_mailboxes.c.org_id == org_id)
                .order_by(inbox_mailboxes.c.address)
            ).all()
        return [{
            "id": str(r.id),
            "address": r.address,
            "connectionName": r.connection_name,
            "status": r.status,
            "lastSyncedAt": _iso(r.last_synced_at),
            "lastError": r.last_error,
            "createdBy": r.created_by,
        } for r in rows]

    def addresses(self, org_id: str) -> set[str]:
        return {m["address"].lower() for m in self.list(org_id)}

    def upsert(self, org_id: str, address: str, connection_name: str,
               user: str | None) -> str:
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(inbox_mailboxes.c.id)
                .where(inbox_mailboxes.c.org_id == org_id)
                .where(inbox_mailboxes.c.address == address)
            ).first()
            if existing:
                conn.execute(
                    update(inbox_mailboxes)
                    .where(inbox_mailboxes.c.id == existing.id)
                    .values(connection_name=connection_name, status="ok",
                            last_error=None)
                )
                return str(existing.id)
            mailbox_id = _uuid()
            conn.execute(insert(inbox_mailboxes).values(
                id=mailbox_id, org_id=org_id, address=address,
                connection_name=connection_name, status="ok",
                created_by=user, created_at=_now(),
            ))
            return mailbox_id

    def record_sync(self, org_id: str, mailbox_id: str,
                    error: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(inbox_mailboxes)
                .where(inbox_mailboxes.c.org_id == org_id)
                .where(inbox_mailboxes.c.id == mailbox_id)
                .values(last_synced_at=_now(), last_error=error,
                        status="erroring" if error else "ok")
            )

    def remove(self, org_id: str, mailbox_id: str) -> str | None:
        """Forget a mailbox. Returns the connection name so its token can go too."""
        with self._engine.begin() as conn:
            row = conn.execute(
                select(inbox_mailboxes.c.connection_name)
                .where(inbox_mailboxes.c.org_id == org_id)
                .where(inbox_mailboxes.c.id == mailbox_id)
            ).first()
            if row is None:
                return None
            conn.execute(
                delete(inbox_mailboxes)
                .where(inbox_mailboxes.c.org_id == org_id)
                .where(inbox_mailboxes.c.id == mailbox_id)
            )
        return row.connection_name


class SettingsStore:
    """Per-org inbox settings, created on demand with safe defaults."""

    def __init__(self, engine):
        self._engine = engine

    def get(self, org_id: str) -> dict[str, Any]:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(inbox_settings)
                .where(inbox_settings.c.org_id == org_id)
            ).first()
            if row is None:
                conn.execute(insert(inbox_settings).values(
                    org_id=org_id, updated_at=_now()
                ))
                return {"lookbackDays": 7, "xeroConnection": "default",
                        "xeroTenantId": None, "classifierModel": None,
                        "updatedBy": None}
        return {
            "lookbackDays": row.lookback_days,
            "xeroConnection": row.xero_connection,
            # Null means "whatever tenant the connection recorded", which is a
            # guess — the consent callback stores the first one Xero lists.
            "xeroTenantId": row.xero_tenant_id,
            "classifierModel": row.classifier_model,
            "updatedBy": row.updated_by,
        }

    def save(self, org_id: str, *, lookback_days: int, xero_connection: str,
             xero_tenant_id: str | None, classifier_model: str | None,
             user: str) -> None:
        self.get(org_id)          # ensure the row exists
        with self._engine.begin() as conn:
            conn.execute(
                update(inbox_settings)
                .where(inbox_settings.c.org_id == org_id)
                .values(lookback_days=lookback_days,
                        xero_connection=xero_connection,
                        xero_tenant_id=xero_tenant_id,
                        classifier_model=classifier_model,
                        updated_by=user, updated_at=_now())
            )


class CategoryStore:
    """The classification buckets. Seeded once, extensible by the org."""

    def __init__(self, engine):
        self._engine = engine

    def seed(self, org_id: str, user: str | None = None) -> None:
        """Give a fresh org the shipped buckets. Idempotent."""
        existing = {c.key for c in self.list(org_id)}
        rows = [{
            "id": _uuid(), "org_id": org_id, "key": c.key, "label": c.label,
            "description": c.description, "is_system": True, "enabled": True,
            "sort_order": c.sort_order, "created_by": user, "created_at": _now(),
        } for c in SYSTEM_CATEGORIES if c.key not in existing]
        if rows:
            with self._engine.begin() as conn:
                conn.execute(insert(inbox_categories), rows)

    def list(self, org_id: str) -> list[CategoryDef]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_categories)
                .where(inbox_categories.c.org_id == org_id)
                .order_by(inbox_categories.c.sort_order, inbox_categories.c.key)
            ).all()
        return [CategoryDef(
            key=r.key, label=r.label, description=r.description,
            is_system=bool(r.is_system), enabled=bool(r.enabled),
            sort_order=r.sort_order,
        ) for r in rows]

    def enabled(self, org_id: str) -> list[CategoryDef]:
        return [c for c in self.list(org_id) if c.enabled]

    def create(self, org_id: str, key: str, label: str, description: str,
               user: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(inbox_categories).values(
                id=_uuid(), org_id=org_id, key=key, label=label,
                description=description, is_system=False, enabled=True,
                sort_order=500, created_by=user, created_at=_now(),
            ))

    def update(self, org_id: str, key: str, **values) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                update(inbox_categories)
                .where(inbox_categories.c.org_id == org_id)
                .where(inbox_categories.c.key == key)
                .values(**values)
            )
        return result.rowcount > 0


class EmailStore:
    def __init__(self, engine):
        self._engine = engine

    def known_ids(self, mailbox_id: str) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_emails.c.gmail_message_id)
                .where(inbox_emails.c.mailbox_id == mailbox_id)
            ).scalars().all()
        return set(rows)

    def insert(self, org_id: str, mailbox_id: str, message: dict[str, Any],
               *, state: str, state_reason: str | None,
               body_stripped: str) -> str | None:
        """Store one message. Returns None if it was already there.

        The uniqueness of (mailbox_id, gmail_message_id) is enforced by the
        database, so a concurrent sync racing this one loses cleanly rather
        than producing a duplicate card.
        """
        email_id = _uuid()
        try:
            with self._engine.begin() as conn:
                conn.execute(insert(inbox_emails).values(
                    id=email_id, org_id=org_id, mailbox_id=mailbox_id,
                    gmail_message_id=message["gmail_message_id"],
                    gmail_thread_id=message.get("gmail_thread_id") or "",
                    rfc822_message_id=message.get("rfc822_message_id"),
                    in_reply_to=message.get("in_reply_to"),
                    email_references=message.get("email_references"),
                    from_name=message.get("from_name"),
                    from_email=message.get("from_email") or "",
                    subject=message.get("subject") or "",
                    body_text=message.get("body_text") or "",
                    body_html=message.get("body_html") or "",
                    body_stripped=body_stripped,
                    snippet=message.get("snippet") or "",
                    has_attachments=bool(message.get("has_attachments")),
                    received_at=message.get("received_at") or _now(),
                    state=state, state_reason=state_reason, created_at=_now(),
                ))
        except IntegrityError:
            return None
        return email_id

    def get(self, org_id: str, email_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.id == email_id)
            ).first()
        return _email_json(row) if row else None

    def set_state(self, org_id: str, email_id: str, state: str,
                  reason: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.id == email_id)
                .values(state=state, state_reason=reason)
            )

    def pending(self, org_id: str) -> list[dict[str, Any]]:
        """Ingested but not yet triaged."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.state == State.RECEIVED)
                .order_by(inbox_emails.c.received_at)
            ).all()
        return [_email_json(r) for r in rows]

    def list(self, org_id: str, states: list[str] | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        stmt = (select(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .order_by(inbox_emails.c.received_at.desc())
                .limit(limit))
        if states:
            stmt = stmt.where(inbox_emails.c.state.in_(states))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_email_json(r) for r in rows]

    def thread(self, org_id: str, thread_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.gmail_thread_id == thread_id)
                .order_by(inbox_emails.c.received_at)
            ).all()
        return [_email_json(r) for r in rows]

    def from_sender(self, org_id: str, sender: str, exclude_id: str,
                    limit: int = 5) -> list[dict[str, Any]]:
        """Recent mail from the same customer, for context without leaving here."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_emails)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.from_email == sender)
                .where(inbox_emails.c.id != exclude_id)
                .order_by(inbox_emails.c.received_at.desc())
                .limit(limit)
            ).all()
        return [_email_json(r) for r in rows]

    def has_open_dispute(self, org_id: str, sender: str, days: int = 90) -> bool:
        """Whether this contact has been classified as disputing recently.

        Deliberately per *contact*, not per thread: a customer mid-dispute who
        writes about a different invoice is still mid-dispute.
        """
        cutoff = _now() - timedelta(days=days)
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_classifications.c.id)
                .select_from(
                    inbox_classifications.join(
                        inbox_emails,
                        inbox_emails.c.id == inbox_classifications.c.email_id)
                )
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.from_email == sender)
                .where(inbox_emails.c.received_at >= cutoff)
                .where(inbox_classifications.c.category_key == "Dispute")
                .limit(1)
            ).first()
        return row is not None

    def stats(self, org_id: str, days: int = 7) -> dict[str, Any]:
        cutoff = _now() - timedelta(days=days)
        with self._engine.connect() as conn:
            emails = conn.execute(
                select(inbox_emails.c.id, inbox_emails.c.state,
                       inbox_emails.c.received_at)
                .where(inbox_emails.c.org_id == org_id)
                .where(inbox_emails.c.received_at >= cutoff)
            ).all()
            replies = conn.execute(
                select(inbox_replies.c.email_id, inbox_replies.c.completed_at)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.status == "created")
            ).all()

        replied_at = {str(r.email_id): r.completed_at for r in replies}
        turnarounds: list[float] = []
        for row in emails:
            done = replied_at.get(str(row.id))
            if done is None:
                continue
            start, end = row.received_at, done
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            turnarounds.append((end - start).total_seconds() / 3600)

        counted = [e for e in emails if e.state != State.SUPPRESSED]
        waiting = [e for e in counted if e.state == State.NEEDS_REVIEW]
        turnarounds.sort()
        median = turnarounds[len(turnarounds) // 2] if turnarounds else None

        return {
            "days": days,
            "received": len(counted),
            "suppressed": len(emails) - len(counted),
            "drafted": len([e for e in counted if e.state == State.DRAFTED]),
            "waiting": len(waiting),
            "medianHoursToDraft": round(median, 1) if median is not None else None,
        }


def _email_json(row) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "mailboxId": str(row.mailbox_id),
        "gmailMessageId": row.gmail_message_id,
        "threadId": row.gmail_thread_id,
        "rfc822MessageId": row.rfc822_message_id,
        "inReplyTo": row.in_reply_to,
        "references": row.email_references,
        "fromName": row.from_name,
        "fromEmail": row.from_email,
        "subject": row.subject,
        "bodyText": row.body_text,
        "bodyStripped": row.body_stripped,
        "snippet": row.snippet,
        "hasAttachments": bool(row.has_attachments),
        "receivedAt": _iso(row.received_at),
        "state": row.state,
        "stateReason": row.state_reason,
    }


class ClassificationStore:
    """Append-only. A human override is a new row, never an edit."""

    def __init__(self, engine):
        self._engine = engine

    def add(self, org_id: str, email_id: str, c: Classification,
            user: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(inbox_classifications).values(
                id=_uuid(), org_id=org_id, email_id=email_id,
                category_key=c.category, confidence=c.confidence,
                secondary_key=c.secondary,
                secondary_confidence=c.secondary_confidence,
                multi_intent=c.multi_intent, language=c.language,
                extracted=c.extracted.to_json(), model_version=c.model_version,
                latency_ms=c.latency_ms, source=c.source, created_by=user,
                created_at=_now(),
            ))

    def history(self, org_id: str, email_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_classifications)
                .where(inbox_classifications.c.org_id == org_id)
                .where(inbox_classifications.c.email_id == email_id)
                .order_by(inbox_classifications.c.created_at.desc())
            ).all()
        return [{
            "categoryKey": r.category_key,
            "confidence": r.confidence,
            "secondaryKey": r.secondary_key,
            "secondaryConfidence": r.secondary_confidence,
            "multiIntent": bool(r.multi_intent),
            "language": r.language,
            "extracted": r.extracted or {},
            "modelVersion": r.model_version,
            "latencyMs": r.latency_ms,
            "source": r.source,
            "createdBy": r.created_by,
            "createdAt": _iso(r.created_at),
        } for r in rows]

    def latest_map(self, org_id: str,
                   email_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Newest classification per email, in one query.

        The list view needs a category chip for every row; asking per row would
        be three hundred queries to draw one screen.
        """
        if not email_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_classifications)
                .where(inbox_classifications.c.org_id == org_id)
                .where(inbox_classifications.c.email_id.in_(email_ids))
                .order_by(inbox_classifications.c.created_at)
            ).all()
        # Ascending, so the last write for each email wins.
        return {str(r.email_id): {
            "categoryKey": r.category_key,
            "confidence": r.confidence,
            "multiIntent": bool(r.multi_intent),
            "source": r.source,
            "extracted": r.extracted or {},
        } for r in rows}

    def latest(self, org_id: str, email_id: str) -> Classification | None:
        rows = self.history(org_id, email_id)
        if not rows:
            return None
        top = rows[0]
        extracted = top.get("extracted") or {}
        return Classification(
            category=top["categoryKey"],
            confidence=top["confidence"] or 0.0,
            secondary=top["secondaryKey"],
            secondary_confidence=top["secondaryConfidence"],
            multi_intent=top["multiIntent"],
            language=top["language"] or "en",
            extracted=Extracted(
                invoice_number=extracted.get("invoiceNumber"),
                amount=extracted.get("amount"),
                currency=extracted.get("currency"),
                customer_name=extracted.get("customerName"),
                mentioned_date=extracted.get("mentionedDate"),
            ),
            model_version=top["modelVersion"] or "",
            source=top["source"],
        )


class LookupStore:
    def __init__(self, engine):
        self._engine = engine

    def add(self, org_id: str, email_id: str, result: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(inbox_lookups).values(
                id=_uuid(), org_id=org_id, email_id=email_id,
                outcome=result.get("outcome"),
                xero_tenant_id=result.get("xeroTenantId"),
                invoice_id=result.get("invoiceId"),
                invoice_number=result.get("invoiceNumber"),
                contact_name=result.get("contactName"),
                amount=result.get("amount"),
                currency=result.get("currency"),
                due_date=result.get("dueDate"),
                description=result.get("description"),
                summary=result.get("summary"),
                outstanding_balance=result.get("outstandingBalance"),
                invoice_status=result.get("invoiceStatus"),
                candidates=result.get("candidates") or [],
                mismatches=result.get("mismatches") or [],
                error=result.get("error"), created_at=_now(),
            ))

    def latest(self, org_id: str, email_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_lookups)
                .where(inbox_lookups.c.org_id == org_id)
                .where(inbox_lookups.c.email_id == email_id)
                .order_by(inbox_lookups.c.created_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return {
            "outcome": row.outcome,
            "xeroTenantId": row.xero_tenant_id,
            "invoiceId": row.invoice_id,
            "invoiceNumber": row.invoice_number,
            "contactName": row.contact_name,
            "amount": row.amount,
            "currency": row.currency,
            "dueDate": row.due_date,
            "description": row.description,
            "summary": row.summary,
            "outstandingBalance": row.outstanding_balance,
            "invoiceStatus": row.invoice_status,
            "candidates": row.candidates or [],
            "mismatches": row.mismatches or [],
            "error": row.error,
            "createdAt": _iso(row.created_at),
        }


class DraftStore:
    def __init__(self, engine):
        self._engine = engine

    def add(self, org_id: str, email_id: str, *, category_key: str,
            template_version: int | None, subject: str, body: str,
            fields: dict[str, Any], missing: list[str],
            blockers: list[dict[str, str]], edited: bool = False,
            user: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(inbox_drafts).values(
                id=_uuid(), org_id=org_id, email_id=email_id,
                category_key=category_key, template_version=template_version,
                subject=subject, body=body, fields=fields,
                missing_slots=missing, blockers=blockers, edited=edited,
                updated_by=user, created_at=_now(),
            ))

    def latest(self, org_id: str, email_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_drafts)
                .where(inbox_drafts.c.org_id == org_id)
                .where(inbox_drafts.c.email_id == email_id)
                .order_by(inbox_drafts.c.created_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return {
            "categoryKey": row.category_key,
            "templateVersion": row.template_version,
            "subject": row.subject,
            "body": row.body,
            "fields": row.fields or {},
            "missingSlots": row.missing_slots or [],
            "blockers": row.blockers or [],
            "edited": bool(row.edited),
            "updatedBy": row.updated_by,
            "createdAt": _iso(row.created_at),
        }


class ReplyStore:
    """One reply per email, enforced by the database.

    `reserve` inserts before Gmail is called. Two tabs, a double click and a
    replayed request all race for the same unique constraint, and exactly one
    of them wins — which is why this is a constraint and not a UI state.
    """

    def __init__(self, engine):
        self._engine = engine

    def reserve(self, org_id: str, email_id: str, *, category_key: str,
                template_version: int | None, subject: str, body: str,
                fields: dict[str, Any], actor: str) -> str | None:
        reply_id = _uuid()
        try:
            with self._engine.begin() as conn:
                conn.execute(insert(inbox_replies).values(
                    id=reply_id, org_id=org_id, email_id=email_id,
                    category_key=category_key,
                    template_version=template_version, subject=subject,
                    body=body, fields=fields, status="pending", attempts=0,
                    actor=actor, created_at=_now(),
                ))
        except IntegrityError:
            return None
        return reply_id

    def claim_failed(self, org_id: str, email_id: str, *, subject: str,
                     body: str, fields: dict[str, Any], category_key: str,
                     actor: str) -> str | None:
        """Take over a previously failed attempt, so a retry is possible.

        Only a failed row may be reclaimed. A row in `created` is final: the
        draft exists in Gmail and making a second one is exactly the duplicate
        this table prevents.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                select(inbox_replies.c.id, inbox_replies.c.status,
                       inbox_replies.c.attempts)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.email_id == email_id)
            ).first()
            if row is None or row.status != "failed":
                return None
            conn.execute(
                update(inbox_replies)
                .where(inbox_replies.c.id == row.id)
                .values(status="pending", subject=subject, body=body,
                        fields=fields, category_key=category_key, actor=actor,
                        error=None)
            )
            return str(row.id)

    def complete(self, org_id: str, reply_id: str, *, draft_id: str,
                 thread_id: str, mailbox_address: str | None,
                 attempts: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(inbox_replies)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.id == reply_id)
                .values(status="created", gmail_draft_id=draft_id,
                        gmail_thread_id=thread_id,
                        mailbox_address=mailbox_address, attempts=attempts,
                        completed_at=_now(), error=None)
            )

    def fail(self, org_id: str, reply_id: str, error: str,
             attempts: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(inbox_replies)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.id == reply_id)
                .values(status="failed", error=error[:500], attempts=attempts)
            )

    def status_map(self, org_id: str, email_ids: list[str]) -> dict[str, str]:
        """Reply status per email, for the list view's badges."""
        if not email_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_replies.c.email_id, inbox_replies.c.status)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.email_id.in_(email_ids))
            ).all()
        return {str(r.email_id): r.status for r in rows}

    def get(self, org_id: str, email_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_replies)
                .where(inbox_replies.c.org_id == org_id)
                .where(inbox_replies.c.email_id == email_id)
            ).first()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "status": row.status,
            "gmailDraftId": row.gmail_draft_id,
            "mailboxAddress": row.mailbox_address,
            "categoryKey": row.category_key,
            "templateVersion": row.template_version,
            # Stored as at draft time and never re-rendered against live Xero.
            # An audit trail that updates itself is not an audit trail.
            "subject": row.subject,
            "body": row.body,
            "fields": row.fields or {},
            "attempts": row.attempts,
            "error": row.error,
            "actor": row.actor,
            "createdAt": _iso(row.created_at),
            "completedAt": _iso(row.completed_at),
        }


class TemplateVersionStore:
    """Append-only wording history, so an audit row can be read back."""

    def __init__(self, engine):
        self._engine = engine

    def add(self, org_id: str, variant: str, subject: str, body: str,
            user: str) -> int:
        with self._engine.begin() as conn:
            latest = conn.execute(
                select(inbox_template_versions.c.version)
                .where(inbox_template_versions.c.org_id == org_id)
                .where(inbox_template_versions.c.variant == variant)
                .order_by(inbox_template_versions.c.version.desc())
                .limit(1)
            ).first()
            version = (latest.version if latest else 0) + 1
            conn.execute(insert(inbox_template_versions).values(
                id=_uuid(), org_id=org_id, variant=variant, version=version,
                subject=subject, body=body, updated_by=user, created_at=_now(),
            ))
        return version

    def current_version(self, org_id: str, variant: str) -> int | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(inbox_template_versions.c.version)
                .where(inbox_template_versions.c.org_id == org_id)
                .where(inbox_template_versions.c.variant == variant)
                .order_by(inbox_template_versions.c.version.desc())
                .limit(1)
            ).first()
        return row.version if row else None


class ErrorStore:
    def __init__(self, engine):
        self._engine = engine

    def add(self, org_id: str, stage: str, code: str, message: str,
            email_id: str | None = None, attempts: int = 1) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(inbox_errors).values(
                id=_uuid(), org_id=org_id, email_id=email_id, stage=stage,
                code=code, message=(message or "")[:500], attempts=attempts,
                resolved=False, created_at=_now(),
            ))

    def open(self, org_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(inbox_errors)
                .where(inbox_errors.c.org_id == org_id)
                .where(inbox_errors.c.resolved.is_(False))
                .order_by(inbox_errors.c.created_at.desc())
                .limit(limit)
            ).all()
        return [{
            "id": str(r.id),
            "emailId": str(r.email_id) if r.email_id else None,
            "stage": r.stage,
            "code": r.code,
            "message": r.message,
            "attempts": r.attempts,
            "createdAt": _iso(r.created_at),
        } for r in rows]

    def resolve_all(self, org_id: str) -> int:
        with self._engine.begin() as conn:
            return conn.execute(
                update(inbox_errors)
                .where(inbox_errors.c.org_id == org_id)
                .where(inbox_errors.c.resolved.is_(False))
                .values(resolved=True)
            ).rowcount
