"""Fill an approved template from verified values. No network, no model.

This module is the mechanical guarantee behind "it never writes a reply in its
own words". It imports nothing that can reach a language model, and every
character it emits comes from either the stored template or a value looked up
in Xero. `unfill()` exists so a test can assert that property rather than a
comment claiming it.

Two rules that are easy to get wrong and highly visible when you do:

  * Money is formatted from the **Xero** currency code, never from whatever
    symbol the customer typed. A customer writing "£3,500" about an invoice
    Xero holds in AUD must not receive a reply mixing the two.
  * The figure a customer cares about is the **outstanding balance**, not the
    invoice total. A part payment or credit note makes them different.
"""

from __future__ import annotations

import re
from typing import Any

from .templates import NON_INVOICE_SLOTS, PLACEHOLDER_NAMES

# {{ name }} — whitespace tolerated, because a human edits these by hand.
_SLOT = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Used when Xero holds no name for the contact. Never a blank, and never the
# literal "None" — both of which have reached real customers in other products.
NEUTRAL_NAME = "there"

# How many line-item descriptions to name before condensing. An invoice with
# forty lines must not become a wall of text in a reply.
MAX_LINE_ITEMS = 3


def slots(text: str) -> list[str]:
    """Placeholder names in the order they appear, without duplicates."""
    seen: list[str] = []
    for name in _SLOT.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def segments(text: str) -> list[tuple[str, str]]:
    """The template split into ('literal', text) and ('slot', name) parts.

    Exposed so the substitution is inspectable: a rendered body is exactly
    these literals with slot values between them, and nothing else.
    """
    out: list[tuple[str, str]] = []
    cursor = 0
    for match in _SLOT.finditer(text or ""):
        if match.start() > cursor:
            out.append(("literal", text[cursor:match.start()]))
        out.append(("slot", match.group(1)))
        cursor = match.end()
    if cursor < len(text or ""):
        out.append(("literal", text[cursor:]))
    return out


def fill(template: str, values: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (rendered, missing_slots).

    A slot with no value is left standing as `{{slot}}` rather than blanked.
    That is deliberate: an empty gap in a draft looks like finished prose and
    gets sent, whereas a visible `{{invoice_number}}` cannot be mistaken for
    anything but an unanswered question. The API refuses to create a draft
    while any remain.
    """
    missing: list[str] = []
    out: list[str] = []
    for kind, part in segments(template):
        if kind == "literal":
            out.append(part)
            continue
        value = values.get(part)
        if value is None or str(value).strip() == "":
            missing.append(part)
            out.append("{{" + part + "}}")
        else:
            out.append(str(value))
    # Preserve first-appearance order without duplicates.
    ordered: list[str] = []
    for name in missing:
        if name not in ordered:
            ordered.append(name)
    return "".join(out), ordered


def unfill(rendered: str, values: dict[str, Any]) -> str:
    """Put the placeholders back, so a test can compare against the template.

    Longest values first: a short value that happens to be a substring of a
    longer one would otherwise eat it and the comparison would fail for the
    wrong reason.
    """
    out = rendered
    usable = [
        (name, str(value))
        for name, value in values.items()
        if value is not None and str(value).strip() != ""
    ]
    for name, value in sorted(usable, key=lambda kv: -len(kv[1])):
        out = out.replace(value, "{{" + name + "}}")
    return out


def unfilled(text: str) -> list[str]:
    """Placeholders still standing in a body. Non-empty blocks drafting.

    Applied to the body a person may have edited by hand, which is why it
    parses the text rather than trusting the missing-slot list computed at
    render time: deleting the placeholder is a legitimate way to unblock a
    draft, and so is typing a new one by mistake.
    """
    return slots(text)


def money(value: Any) -> str | None:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return None


def describe(line_items: list[dict[str, Any]] | None,
             fallback: str | None = None) -> tuple[str | None, str | None]:
    """Return (description, summary) from an invoice's line items.

    Both are built here, in the application layer, from text Xero already
    holds. Nothing is generated: the "summary" is the first few line
    descriptions plus a count of the rest, which is what stops a forty-line
    invoice becoming a wall of text in a reply.
    """
    names = [
        str(item.get("Description") or "").strip()
        for item in (line_items or [])
        if str(item.get("Description") or "").strip()
    ]
    if not names:
        text = (fallback or "").strip() or None
        return text, text

    description = ", ".join(names)
    if len(names) <= MAX_LINE_ITEMS:
        return description, description

    head = ", ".join(names[:MAX_LINE_ITEMS])
    rest = len(names) - MAX_LINE_ITEMS
    summary = f"{head}, and {rest} further item{'s' if rest != 1 else ''}"
    return description, summary


def field_map(lookup: dict[str, Any] | None, sender: str,
              customer_name: str | None = None) -> dict[str, Any]:
    """Values for the template slots.

    `lookup` is the Xero record — the only source for anything factual.
    `customer_name` is the AI-extracted name, used *only* when Xero holds no
    name for the contact, and never in preference to it.
    """
    found = lookup or {}
    currency = (found.get("currency") or "").strip() or None

    values: dict[str, Any] = {
        "customer_name": (
            (found.get("contactName") or "").strip()
            or (customer_name or "").strip()
            or NEUTRAL_NAME
        ),
        "sender": sender or "Accounts",
        "invoice_number": (found.get("invoiceNumber") or "").strip() or None,
        "currency": currency,
        "amount": money(found.get("amount")),
        "outstanding_balance": money(found.get("outstandingBalance")),
        "due_date": (found.get("dueDate") or "").strip() or None,
        "status": (found.get("invoiceStatus") or "").strip() or None,
        "description": (found.get("description") or "").strip() or None,
        "summary": (found.get("summary") or "").strip() or None,
        "invoice_pdf": (found.get("invoicePdf") or "").strip() or None,
    }
    return values


def required_invoice_slots(template_body: str, template_subject: str = "") -> list[str]:
    """Slots in this template that only an invoice record can fill."""
    used = set(slots(template_body)) | set(slots(template_subject))
    return sorted(
        name for name in used
        if name not in NON_INVOICE_SLOTS and name in PLACEHOLDER_NAMES
    )
