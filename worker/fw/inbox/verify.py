"""The customer's claim, checked against the ledger.

Xero is the source of truth for every value that reaches a customer. What the
AI extracted is only ever a hint about *which* record to fetch, and is shown
beside the authoritative value rather than instead of it.

Three rules worth stating because each has an obvious wrong version:

  * An ambiguous match is a **not found**, never "first result wins". A reused
    or repeating invoice number that resolves to two records means we do not
    know which one the customer means, and guessing produces a confidently
    wrong reply.

  * The lookup is **never skipped**. A timeout routes the email to a person
    carrying a timeout warning; it does not fall through to drafting against
    whatever the customer happened to type.

  * The figure injected is the **outstanding balance**. A credit note or part
    payment makes it differ from the invoice total, and quoting the total to
    someone who has already paid half of it is the most visible way to get
    this wrong.
"""

from __future__ import annotations

from typing import Any

from ..xero import XeroClient, XeroError
from .models import Outcome
from .render import describe

# Xero's own timeout is generous; this one is the product's promise. Ten
# seconds, then a person looks at it.
TIMEOUT_SECONDS = 10

# Tolerance when comparing what the customer said to what Xero holds. A penny
# of rounding in a currency conversion is not a mismatch worth flagging.
AMOUNT_TOLERANCE = 0.01

ONLINE_INVOICE_URL = "https://in.xero.com/{short_code}"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    from ..workflows.overdue_chaseup import parse_xero_date

    parsed = parse_xero_date(value)
    return parsed.isoformat() if parsed else None


class InvoiceLookup:
    """Reads invoices for one org's Xero connection. Read-only, by scope."""

    def __init__(self, client: XeroClient, tenant_id: str):
        self._client = client
        self._tenant_id = tenant_id

    def by_number(self, number: str) -> list[dict[str, Any]]:
        found = self._client.get_invoices_by_number(number)
        # Xero matches loosely enough that a request for "1042" can return
        # "INV-1042" and "1042-A". Only an exact, case-insensitive match counts.
        wanted = number.strip().lower()
        return [
            inv for inv in found
            if str(inv.get("InvoiceNumber") or "").strip().lower() == wanted
        ]

    def open_for_sender(self, email: str) -> list[dict[str, Any]]:
        """Open invoices for whoever sent the email, by address then domain.

        This is what surfaces the right invoice when a customer quotes their
        own purchase-order number instead of ours: no match by design, and a
        short list for the manager to pick from.
        """
        if not email or "@" not in email:
            return []
        contacts = self._client.find_contacts_by_email(email)
        if not contacts:
            domain = email.split("@", 1)[1]
            contacts = self._client.find_contacts_by_domain(domain)
        invoices: list[dict[str, Any]] = []
        for contact in contacts[:5]:
            cid = contact.get("ContactID")
            if cid:
                invoices.extend(self._client.get_open_invoices_for_contact(cid))
        return invoices


def snapshot(invoice: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """The authoritative record, in the shape the template renderer wants."""
    contact = invoice.get("Contact") or {}
    description, summary = describe(
        invoice.get("LineItems"), invoice.get("Reference")
    )
    short_code = str(contact.get("ContactID") or "")
    online = invoice.get("OnlineInvoiceUrl") or (
        ONLINE_INVOICE_URL.format(short_code=short_code) if short_code else None
    )
    return {
        "xeroTenantId": tenant_id,
        "invoiceId": invoice.get("InvoiceID"),
        "invoiceNumber": str(invoice.get("InvoiceNumber") or "").strip(),
        "contactName": str(contact.get("Name") or "").strip(),
        "amount": _to_float(invoice.get("Total")),
        "currency": str(invoice.get("CurrencyCode") or "").strip(),
        "dueDate": _date(invoice.get("DueDateString") or invoice.get("DueDate")),
        "description": description,
        "summary": summary,
        "outstandingBalance": _to_float(invoice.get("AmountDue")),
        "invoiceStatus": str(invoice.get("Status") or "").strip(),
        "invoicePdf": online,
    }


def compare(record: dict[str, Any], extracted: Any) -> list[dict[str, Any]]:
    """Where the customer and Xero disagree. Both values are kept.

    The review pane preselects Xero and requires an explicit confirmation to
    use the customer's figure — and logs both either way.
    """
    out: list[dict[str, Any]] = []

    claimed_number = (getattr(extracted, "invoice_number", None) or "").strip()
    actual_number = (record.get("invoiceNumber") or "").strip()
    if claimed_number and actual_number and \
            claimed_number.lower() != actual_number.lower():
        out.append({"field": "invoiceNumber", "claimed": claimed_number,
                    "actual": actual_number})

    claimed_amount = getattr(extracted, "amount", None)
    if claimed_amount is not None:
        # Compared against the outstanding balance as well as the total: a
        # customer quoting what they still owe is not in disagreement with us.
        candidates = [record.get("amount"), record.get("outstandingBalance")]
        if not any(
            value is not None and abs(float(value) - float(claimed_amount))
            <= AMOUNT_TOLERANCE
            for value in candidates
        ):
            out.append({
                "field": "amount",
                "claimed": claimed_amount,
                "actual": record.get("amount"),
                "outstanding": record.get("outstandingBalance"),
            })

    return out


def lookup(client: XeroClient, tenant_id: str, extracted: Any,
           sender_email: str) -> dict[str, Any]:
    """Resolve the invoice this email is about.

    Always returns a row to record — outcome included — because "we did not
    look" and "we looked and found nothing" are different facts and the second
    one is the only acceptable state to draft from.
    """
    finder = InvoiceLookup(client, tenant_id)
    number = (getattr(extracted, "invoice_number", None) or "").strip()

    base: dict[str, Any] = {
        "outcome": Outcome.SKIPPED,
        "xeroTenantId": tenant_id,
        "candidates": [],
        "mismatches": [],
        "error": None,
    }

    try:
        matches = finder.by_number(number) if number else []

        if len(matches) == 1:
            record = snapshot(matches[0], tenant_id)
            record.update({
                "outcome": Outcome.FOUND,
                "candidates": [],
                "mismatches": compare(record, extracted),
                "error": None,
            })
            return record

        # No single match. Offer the sender's open invoices either way, so the
        # manager has something to pick from rather than a dead end.
        candidates = [
            snapshot(inv, tenant_id)
            for inv in finder.open_for_sender(sender_email)[:10]
        ]
        base["candidates"] = candidates

        if len(matches) > 1:
            # Two records answering to one number. Treated exactly as not
            # found: we do not know which, so we do not choose.
            base["outcome"] = Outcome.AMBIGUOUS
            base["candidates"] = [snapshot(m, tenant_id) for m in matches]
            base["error"] = (
                f"{len(matches)} invoices in Xero share the number {number}."
            )
            return base

        base["outcome"] = Outcome.NOT_FOUND if number else Outcome.SKIPPED
        if number:
            base["error"] = f"No invoice numbered {number} exists in Xero."
        return base

    except XeroError as exc:
        message = str(exc)
        timed_out = "timed out" in message.lower() or "timeout" in message.lower()
        base["outcome"] = Outcome.TIMED_OUT if timed_out else Outcome.ERROR
        base["error"] = message[:500]
        return base
