"""Xero is the source of truth, and an uncertain match is not a match.

The expensive mistake this prevents: a customer quotes a number that resolves
to two invoices, the product picks the first, and the reply confidently
describes the wrong one. "Ambiguous" must behave exactly like "not found".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.inbox import verify  # noqa: E402
from fw.inbox.models import Extracted, Outcome  # noqa: E402
from fw.xero import XeroError  # noqa: E402

TENANT = "tenant-abc"


def invoice(number="INV-1042", total=3050.0, due=1525.0, **extra):
    base = {
        "InvoiceID": f"id-{number}",
        "InvoiceNumber": number,
        "Contact": {"ContactID": "c1", "Name": "Northwind Trading"},
        "Total": total,
        "AmountDue": due,
        "CurrencyCode": "AUD",
        "DueDateString": "2026-07-31T00:00:00",
        "Status": "AUTHORISED",
        "LineItems": [{"Description": "Platform subscription, June 2026"}],
    }
    base.update(extra)
    return base


class FakeXero:
    """Stands in for the Accounting API. Records what it was asked."""

    def __init__(self, by_number=None, contacts=None, open_invoices=None,
                 raises=None):
        self._by_number = by_number or []
        self._contacts = contacts or []
        self._open = open_invoices or []
        self._raises = raises
        self.calls: list[str] = []

    def get_invoices_by_number(self, number):
        self.calls.append(f"by_number:{number}")
        if self._raises:
            raise self._raises
        return list(self._by_number)

    def find_contacts_by_email(self, email):
        self.calls.append(f"by_email:{email}")
        return list(self._contacts)

    def find_contacts_by_domain(self, domain):
        self.calls.append(f"by_domain:{domain}")
        return []

    def get_open_invoices_for_contact(self, contact_id):
        self.calls.append(f"open:{contact_id}")
        return list(self._open)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def test_a_single_match_is_found_and_carries_the_whole_record():
    client = FakeXero(by_number=[invoice()])
    result = verify.lookup(client, TENANT, Extracted(invoice_number="INV-1042"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.FOUND
    assert result["invoiceNumber"] == "INV-1042"
    assert result["currency"] == "AUD"
    assert result["amount"] == 3050.0
    assert result["outstandingBalance"] == 1525.0
    assert result["dueDate"] == "2026-07-31"
    assert result["invoiceStatus"] == "AUTHORISED"
    assert result["description"] == "Platform subscription, June 2026"
    assert result["xeroTenantId"] == TENANT


def test_two_matches_are_ambiguous_and_never_first_result_wins():
    """A repeating invoice or a reused number. We do not know which, so we do
    not choose."""
    client = FakeXero(by_number=[invoice(), invoice(total=999.0)])
    result = verify.lookup(client, TENANT, Extracted(invoice_number="INV-1042"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.AMBIGUOUS
    assert result.get("invoiceNumber") is None
    assert "2 invoices" in result["error"]


def test_a_number_that_does_not_exist_is_not_found():
    client = FakeXero(by_number=[])
    result = verify.lookup(client, TENANT, Extracted(invoice_number="INV-9999"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.NOT_FOUND
    assert "INV-9999" in result["error"]


def test_a_loose_xero_match_is_rejected():
    """Xero answers a request for 1042 with INV-1042 and 1042-A. Only an exact
    match is the invoice the customer meant."""
    client = FakeXero(by_number=[invoice("INV-1042"), invoice("1042-A")])
    result = verify.lookup(client, TENANT, Extracted(invoice_number="1042"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.NOT_FOUND


def test_no_invoice_number_means_the_lookup_is_skipped_not_guessed():
    client = FakeXero()
    result = verify.lookup(client, TENANT, Extracted(), "bob@customer.test")

    assert result["outcome"] == Outcome.SKIPPED
    assert "by_number" not in " ".join(client.calls)


def test_a_timeout_is_recorded_as_a_timeout():
    """Distinguished from a failure because they mean different things to
    someone deciding whether to retry."""
    client = FakeXero(raises=XeroError("Xero timed out after 10s on Invoices."))
    result = verify.lookup(client, TENANT, Extracted(invoice_number="INV-1"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.TIMED_OUT


def test_other_xero_failures_are_recorded_as_errors():
    client = FakeXero(raises=XeroError("Xero API error 500 on Invoices"))
    result = verify.lookup(client, TENANT, Extracted(invoice_number="INV-1"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.ERROR


# ---------------------------------------------------------------------------
# The PO-number case
# ---------------------------------------------------------------------------

def test_a_customers_own_po_number_surfaces_their_open_invoices():
    """No match by design; the contact lookup gives the manager a shortlist."""
    client = FakeXero(
        by_number=[],
        contacts=[{"ContactID": "c1"}],
        open_invoices=[invoice("INV-1042"), invoice("INV-1043")],
    )
    result = verify.lookup(client, TENANT, Extracted(invoice_number="PO-88213"),
                           "bob@customer.test")

    assert result["outcome"] == Outcome.NOT_FOUND
    assert [c["invoiceNumber"] for c in result["candidates"]] == \
        ["INV-1042", "INV-1043"]


# ---------------------------------------------------------------------------
# Mismatches
# ---------------------------------------------------------------------------

def test_a_wrong_amount_is_flagged_with_both_values_kept():
    """The specified case: customer says 3,500, Xero says 3,050."""
    client = FakeXero(by_number=[invoice(total=3050.0, due=3050.0)])
    result = verify.lookup(
        client, TENANT,
        Extracted(invoice_number="INV-1042", amount=3500.0),
        "bob@customer.test",
    )

    assert result["outcome"] == Outcome.FOUND
    mismatch = result["mismatches"][0]
    assert mismatch["field"] == "amount"
    assert mismatch["claimed"] == 3500.0
    assert mismatch["actual"] == 3050.0


def test_a_customer_quoting_what_they_still_owe_is_not_a_mismatch():
    """Part payment: they say 1,525 because that is what is left."""
    client = FakeXero(by_number=[invoice(total=3050.0, due=1525.0)])
    result = verify.lookup(
        client, TENANT,
        Extracted(invoice_number="INV-1042", amount=1525.0),
        "bob@customer.test",
    )
    assert result["mismatches"] == []


def test_rounding_is_not_a_mismatch():
    client = FakeXero(by_number=[invoice(total=3050.0, due=3050.0)])
    result = verify.lookup(
        client, TENANT,
        Extracted(invoice_number="INV-1042", amount=3050.005),
        "bob@customer.test",
    )
    assert result["mismatches"] == []


def test_a_long_invoice_gets_a_condensed_summary_alongside_the_description():
    many = [{"Description": f"Seat {n}"} for n in range(40)]
    record = verify.snapshot(invoice(LineItems=many), TENANT)

    assert len(record["summary"]) < len(record["description"])
    assert "further items" in record["summary"]
