"""One email, end to end: ingest, classify, verify, render.

Called from the Sync button and from a category override. Everything it does
is idempotent at the database level, so pressing Sync twice costs a little
Gmail quota and changes nothing else.

The order is not negotiable. Suppression before classification, because an
out-of-office must never be paid for or replied to. Verification before
rendering, because a template is only allowed to quote values that came back
from Xero. Rendering outside the model path entirely, because that is the whole
promise of the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..google import GoogleError
from ..xero import XeroClient, XeroError
from . import classify as classifier
from . import render, review, templates
from .gmail import Mailbox
from .models import Classification, Code, Outcome, State
from .text import display_name, strip_quoted, html_to_text, suppression_reason
from .verify import lookup as xero_lookup


# How many emails one sync will classify. Ingestion is already capped; this
# caps the expensive half. A backlog — after a classifier outage, or after
# re-triaging what an outage mislabelled — would otherwise put a hundred model
# calls and a hundred Xero lookups inside a single foreground request, which
# times out and leaves the person no way to tell how far it got.
MAX_TRIAGE_PER_SYNC = 25


@dataclass
class SyncReport:
    """What one press of the Sync button did."""

    fetched: int = 0
    ingested: int = 0
    suppressed: int = 0
    triaged: int = 0
    failed: int = 0
    more_waiting: bool = False
    untriaged: int = 0
    mailbox_errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "ingested": self.ingested,
            "suppressed": self.suppressed,
            "triaged": self.triaged,
            "failed": self.failed,
            "moreWaiting": self.more_waiting,
            "untriaged": self.untriaged,
            "mailboxErrors": self.mailbox_errors,
        }


class Pipeline:
    """Holds the stores and clients one org's sync needs."""

    def __init__(self, *, org_id: str, stores: dict[str, Any],
                 classification_client, xero: XeroClient | None,
                 xero_tenant_id: str | None, sender_name: str,
                 template_source, log=print):
        self.org_id = org_id
        self.s = stores
        self._classifier = classification_client
        self._xero = xero
        self._tenant_id = xero_tenant_id
        self._sender = sender_name
        self._templates = template_source
        self._log = log

    # -- ingest -------------------------------------------------------------

    def ingest_mailbox(self, mailbox: dict[str, Any], client: Mailbox,
                       lookback_days: int, own_addresses: set[str],
                       report: SyncReport) -> list[str]:
        """Pull recent messages into inbox_emails. Returns new email ids."""
        known = self.s["emails"].known_ids(mailbox["id"])
        # Filtering happens inside the listing, so each sync reaches further
        # back rather than re-reading the newest page for ever.
        fresh, more = client.list_recent(lookback_days, known=known)
        report.fetched += len(fresh)
        if more:
            report.more_waiting = True

        created: list[str] = []
        for message_id in fresh:
            try:
                message = client.fetch(message_id)
            except GoogleError as exc:
                report.failed += 1
                self.s["errors"].add(self.org_id, "ingest", Code.ING_FETCH_FAILED,
                                     f"{message_id}: {exc}")
                continue

            name, address = display_name(message.get("from", ""))
            message["from_name"] = name
            message["from_email"] = address

            reason = suppression_reason(
                message.get("headers") or {}, address, own_addresses
            )

            text_body = message.get("body_text") or html_to_text(
                message.get("body_html") or ""
            )
            stripped = strip_quoted(text_body)

            email_id = self.s["emails"].insert(
                self.org_id, mailbox["id"], message,
                state=State.SUPPRESSED if reason else State.RECEIVED,
                state_reason=reason,
                body_stripped=stripped,
            )
            if email_id is None:
                continue        # raced by another sync; the constraint won
            if reason:
                report.suppressed += 1
            else:
                created.append(email_id)
                report.ingested += 1
        return created

    # -- triage -------------------------------------------------------------

    def classify(self, email: dict[str, Any]) -> Classification:
        """Label one email, or fall back so it still reaches a person."""
        categories = self.s["categories"].enabled(self.org_id)
        if self._classifier is None:
            return classifier.fallback("unconfigured")

        import time

        started = time.monotonic()
        try:
            raw = self._classifier.classify(
                email.get("subject") or "",
                email.get("bodyStripped") or email.get("bodyText") or "",
                categories,
            )
        except classifier.ClassificationError as exc:
            self.s["errors"].add(self.org_id, "classify", exc.code, str(exc),
                                 email_id=email["id"])
            return classifier.fallback(exc.code)

        latency = int((time.monotonic() - started) * 1000)
        result = classifier.coerce(
            raw, categories, self._classifier.model_version, latency
        )
        if result.category == classifier.OUT_OF_SCOPE and \
                str(raw.get("category") or "") not in {c.key for c in categories}:
            # The model answered off-schema. Recorded rather than silently
            # absorbed, so a pattern of it is visible.
            self.s["errors"].add(
                self.org_id, "classify", Code.CLS_BAD_CATEGORY,
                f"Model returned {raw.get('category')!r}", email_id=email["id"],
            )
        return result

    def verify(self, email: dict[str, Any],
               classification: Classification) -> dict[str, Any]:
        """Ask Xero. Never skipped, and the outcome is always recorded."""
        if self._xero is None or not self._tenant_id:
            return {
                "outcome": Outcome.ERROR,
                "candidates": [], "mismatches": [],
                "error": "Xero is not connected for this organisation.",
            }
        try:
            return xero_lookup(self._xero, self._tenant_id,
                               classification.extracted,
                               email.get("fromEmail") or "")
        except XeroError as exc:
            self.s["errors"].add(self.org_id, "verify", Code.XRO_FAILED,
                                 str(exc), email_id=email["id"])
            return {
                "outcome": Outcome.ERROR, "candidates": [], "mismatches": [],
                "error": str(exc)[:500],
            }

    def draft(self, email: dict[str, Any], category_key: str,
              found: dict[str, Any], classification: Classification | None,
              user: str | None = None) -> dict[str, Any]:
        """Render the approved template. No model can reach this code path."""
        template = self._templates(category_key)
        values = render.field_map(
            found, self._sender,
            customer_name=(classification.extracted.customer_name
                           if classification else None),
        )
        subject, subject_missing = render.fill(template["subject"], values)
        body, body_missing = render.fill(template["body"], values)
        missing = sorted(set(subject_missing) | set(body_missing))

        blocking = review.blockers(subject, body, email.get("fromEmail"))
        self.s["drafts"].add(
            self.org_id, email["id"], category_key=category_key,
            template_version=self.s["versions"].current_version(
                self.org_id, category_key),
            subject=subject, body=body, fields=values, missing=missing,
            blockers=blocking, user=user,
        )
        return {"subject": subject, "body": body, "missing": missing,
                "blockers": blocking}

    def triage(self, email: dict[str, Any]) -> None:
        """Classify, verify and draft one ingested email."""
        classification = self.classify(email)
        self.s["classifications"].add(self.org_id, email["id"], classification)

        found = self.verify(email, classification)
        self.s["lookups"].add(self.org_id, email["id"], found)

        self.draft(email, classification.category, found, classification)
        self.s["emails"].set_state(self.org_id, email["id"], State.NEEDS_REVIEW)

    def triage_pending(self, report: SyncReport) -> None:
        waiting = self.s["emails"].pending(self.org_id)
        if len(waiting) > MAX_TRIAGE_PER_SYNC:
            # Oldest first, so a backlog drains in the order it arrived.
            # Reported separately from `more_waiting`: "there is more mail to
            # fetch" and "there is more mail to categorise" are different
            # facts, and a person acts on them the same way but should not be
            # told the first when only the second is true.
            report.untriaged = len(waiting) - MAX_TRIAGE_PER_SYNC
            waiting = waiting[:MAX_TRIAGE_PER_SYNC]

        for email in waiting:
            try:
                self.triage(email)
                report.triaged += 1
            except Exception as exc:            # noqa: BLE001
                # One bad email must not abandon the rest of the sync.
                report.failed += 1
                self.s["errors"].add(
                    self.org_id, "triage", Code.ING_PARSE_FAILED,
                    f"{type(exc).__name__}: {exc}", email_id=email["id"],
                )
