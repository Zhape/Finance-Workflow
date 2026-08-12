"""The guarantee that no outbound word is invented.

The product's central claim is that a customer only ever receives approved
wording with facts dropped into it. That claim is worth exactly as much as the
test that enforces it, which is `test_rendering_adds_nothing_to_the_template`:
strip the injected values back out of a rendered body and what remains must be
the stored template, byte for byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.inbox import render, review, templates  # noqa: E402

XERO_RECORD = {
    "invoiceNumber": "INV-1042",
    "contactName": "Northwind Trading",
    "amount": 3050.0,
    "currency": "AUD",
    "dueDate": "2026-07-31",
    "description": "Platform subscription, June 2026",
    "summary": "Platform subscription, June 2026",
    "outstandingBalance": 1525.0,
    "invoiceStatus": "AUTHORISED",
    "invoicePdf": "https://in.xero.com/abc123",
}


# ---------------------------------------------------------------------------
# The mechanical guarantee
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", sorted(templates.default_templates()))
def test_rendering_adds_nothing_to_the_template(variant):
    """Rendered body minus injected values is the template, byte for byte.

    This is the actual mechanism behind "the AI never writes the reply". If it
    ever fails, something is composing text rather than substituting it.
    """
    template = templates.default_templates()[variant]
    values = render.field_map(XERO_RECORD, sender="Peter Zha")

    body, missing = render.fill(template["body"], values)
    assert not missing, f"{variant} could not be filled from a complete record"

    assert render.unfill(body, values) == template["body"]


def test_every_shipped_template_only_uses_known_placeholders():
    """A slot nothing can fill would reach a customer as literal braces."""
    known = set(templates.PLACEHOLDER_NAMES)
    for variant, template in templates.default_templates().items():
        used = set(render.slots(template["body"])) | \
            set(render.slots(template["subject"]))
        assert used <= known, f"{variant} uses unknown slots: {used - known}"


def test_every_category_ships_with_a_template():
    """A fresh tenant must work with zero configuration."""
    from fw.inbox.models import SYSTEM_CATEGORIES

    shipped = templates.default_templates()
    for category in SYSTEM_CATEGORIES:
        assert category.key in shipped, f"no template for {category.key}"
        assert shipped[category.key]["body"].strip()


# ---------------------------------------------------------------------------
# Unfilled slots
# ---------------------------------------------------------------------------

def test_a_missing_value_stays_visible_rather_than_blanking():
    """An empty gap reads as finished prose. A placeholder cannot be mistaken."""
    values = render.field_map(None, sender="Peter")
    body, missing = render.fill(
        templates.DEFAULT_BODIES["InvoiceQuery"], values
    )

    assert "invoice_number" in missing
    assert "{{invoice_number}}" in body
    assert "None" not in body


def test_an_unfilled_slot_blocks_the_draft():
    blockers = review.blockers("Re: {{invoice_number}}", "Hi there,",
                              "customer@example.com")
    assert [b["code"] for b in blockers] == ["unfilled_slots"]


def test_deleting_the_placeholder_unblocks_it():
    """The specified escape hatch: remove the slot text and it can be sent."""
    assert review.blockers("Re: your invoice", "Hi there, all sorted.",
                           "customer@example.com") == []


def test_no_reply_address_blocks_the_draft():
    codes = [b["code"] for b in review.blockers("Re: x", "Hi", "")]
    assert "no_recipient" in codes


# ---------------------------------------------------------------------------
# The details that are easy to get wrong
# ---------------------------------------------------------------------------

def test_currency_comes_from_xero_not_from_the_customer():
    """Xero says AUD; the customer wrote £. The reply must not mix them."""
    values = render.field_map(XERO_RECORD, sender="Peter")
    body, _ = render.fill(templates.DEFAULT_BODIES["InvoiceQuery"], values)

    assert "AUD" in body
    assert "£" not in body


def test_the_invoice_query_quotes_the_outstanding_balance():
    """A part payment makes the balance differ from the total. Quoting the
    total to someone who has paid half of it is the visible failure."""
    values = render.field_map(XERO_RECORD, sender="Peter")
    body, _ = render.fill(templates.DEFAULT_BODIES["InvoiceQuery"], values)

    assert "1,525.00" in body
    assert "3,050.00" not in body


def test_a_missing_contact_name_falls_back_to_a_neutral_greeting():
    record = dict(XERO_RECORD, contactName="")
    values = render.field_map(record, sender="Peter")

    assert values["customer_name"] == render.NEUTRAL_NAME
    body, missing = render.fill(templates.DEFAULT_BODIES["Confirmation"], values)
    assert "customer_name" not in missing
    assert "None" not in body
    assert "Hi there," in body


def test_the_extracted_name_is_only_used_when_xero_has_none():
    """Xero always wins. The customer's signature is a last resort."""
    with_xero = render.field_map(XERO_RECORD, "Peter", customer_name="Bob")
    assert with_xero["customer_name"] == "Northwind Trading"

    without = render.field_map(dict(XERO_RECORD, contactName=""), "Peter",
                               customer_name="Bob")
    assert without["customer_name"] == "Bob"


def test_forty_line_items_are_condensed_rather_than_listed():
    """The wall-of-text case, solved without a model."""
    line_items = [{"Description": f"Licence seat {n}"} for n in range(40)]
    description, summary = render.describe(line_items)

    assert description.count(",") == 39
    assert summary.endswith("and 37 further items")
    assert len(summary) < len(description)


def test_a_single_line_item_is_not_condensed():
    description, summary = render.describe([{"Description": "Annual support"}])
    assert description == summary == "Annual support"


def test_line_items_fall_back_to_the_invoice_reference():
    description, summary = render.describe([], fallback="PO 88213")
    assert description == summary == "PO 88213"


def test_money_is_formatted_with_thousands_and_two_decimals():
    assert render.money(1234567.5) == "1,234,567.50"
    assert render.money(None) is None
    assert render.money("not a number") is None


def test_unfilled_reads_the_edited_body_not_the_render_time_list():
    """A person can type a placeholder in by hand; that must block too."""
    assert render.unfilled("Hi, about {{invoice_number}}") == ["invoice_number"]
