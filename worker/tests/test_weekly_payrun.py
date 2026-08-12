"""Offline tests for the pay-run business rules.

These are the rules that took real time to get right in the desktop app and
that a hosted version must not quietly change.  No network, no spreadsheet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.banking import TableBankDetails  # noqa: E402
from fw.contract import RunContext, RunStatus  # noqa: E402
from fw.workflows import weekly_payrun as wp  # noqa: E402


class FakeCreds:
    def xero(self, connection: str):
        return ("fake-token", "fake-tenant")


class FakeXero:
    def __init__(self, bills):
        self._bills = bills

    def __call__(self, *_args, **_kwargs):
        return self

    def get_bills(self, statuses=None):
        return self._bills


def bill(name, amount, currency="GBP", ref="INV-1", type_="ACCPAY", due="2026-08-20"):
    return {
        "InvoiceID": f"{name}-{ref}",
        "Type": type_,
        "CurrencyCode": currency,
        "AmountDue": amount,
        "Reference": ref,
        "DueDateString": f"{due}T00:00:00",
        "Contact": {"Name": name, "EmailAddress": f"{name.split()[0].lower()}@x.com"},
    }


BANK = TableBankDetails([
    {"vendor": "Acme Ltd", "Sort Code": "20-00-00", "Account Number": 12345678},
    {"vendor": "Jane Smith", "Sort Code": "30-00-00", "Account Number": 999},
])


def make_ctx():
    return RunContext(org_id="t", creds=FakeCreds(), bank_details=BANK,
                      log=lambda m: None)


def run(bills, monkeypatch, **params):
    monkeypatch.setattr(wp, "XeroClient", FakeXero(bills))
    return wp.run({"region": "UK", **params}, make_ctx())


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def test_expenses_reference_collapses():
    assert wp.clean_reference("July Expenses reimbursement") == "expenses"
    assert wp.clean_reference("  INV-204 ") == "INV-204"


@pytest.mark.parametrize("name,expected", [
    ("Acme Ltd", "Business"),
    ("HMRC", "Business"),
    ("Smith & Jones", "Business"),
    ("Jane Smith", "Person"),
])
def test_receiver_type(name, expected):
    assert wp.receiver_type(name) == expected


def test_parse_xero_microsoft_date():
    # 1518505200000 ms = 2018-02-13 UTC. Must not roll back a day.
    assert wp.parse_xero_date("/Date(1518505200000+0000)/").isoformat() == "2018-02-13"


def test_parse_xero_iso_date():
    assert wp.parse_xero_date("2026-06-19T00:00:00").isoformat() == "2026-06-19"


def test_uk_account_numbers_keep_leading_zeros():
    # Excel stores 00123456 as the number 123456; the bank rejects 6 digits.
    src = TableBankDetails([{"vendor": "A", "Account Number": 123456}])
    assert src.lookup("A")["accountnumber"] == "00123456"


def test_vendor_lookup_is_case_and_space_insensitive():
    assert BANK.lookup("  acme ltd  ")["sortcode"] == "20-00-00"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_ar_invoices_are_excluded(monkeypatch):
    result = run([
        bill("Acme Ltd", 100),
        bill("Acme Ltd", 500, ref="AR-1", type_="ACCREC"),
    ], monkeypatch)
    assert len(result.rows) == 1
    assert result.rows[0]["amount"] == 100


def test_other_currencies_are_excluded(monkeypatch):
    result = run([
        bill("Acme Ltd", 100, currency="GBP"),
        bill("Acme Ltd", 200, currency="USD", ref="INV-2"),
    ], monkeypatch)
    assert [r["amount"] for r in result.rows] == [100]


def test_unmatched_vendors_are_withheld_and_warned(monkeypatch):
    result = run([
        bill("Acme Ltd", 100),
        bill("Unknown Vendor", 50, ref="INV-9"),
    ], monkeypatch)
    assert [r["name"] for r in result.rows] == ["Acme Ltd"]
    assert "Unknown Vendor" in result.warnings[0]


def test_unmatched_can_be_included_deliberately(monkeypatch):
    result = run([bill("Unknown Vendor", 50)], monkeypatch, include_unmatched=True)
    assert len(result.rows) == 1
    assert result.rows[0]["matched"] is False


def test_due_before_cutoff(monkeypatch):
    result = run([
        bill("Acme Ltd", 100, due="2026-08-10"),
        bill("Acme Ltd", 200, ref="INV-2", due="2026-09-10"),
    ], monkeypatch, due_before="2026-08-31")
    assert [r["amount"] for r in result.rows] == [100]


def test_no_bills_is_empty_not_error(monkeypatch):
    result = run([], monkeypatch)
    assert result.status is RunStatus.EMPTY


# ---------------------------------------------------------------------------
# Region conventions
# ---------------------------------------------------------------------------

def test_us_consolidates_by_vendor_and_pays_target_currency(monkeypatch):
    bank = TableBankDetails([{"vendor": "Acme Inc", "abartn": "021000021",
                              "Account Number": "555"}])
    monkeypatch.setattr(wp, "XeroClient", FakeXero([
        bill("Acme Inc", 100, currency="USD", ref="INV-1"),
        bill("Acme Inc", 250, currency="USD", ref="INV-2"),
    ]))
    ctx = RunContext(org_id="t", creds=FakeCreds(), bank_details=bank,
                     log=lambda m: None)
    result = wp.run({"region": "US"}, ctx)

    assert len(result.rows) == 1
    assert result.rows[0]["amount"] == 350
    assert result.rows[0]["reference"] == "INV-1, INV-2"
    assert result.rows[0]["amount_currency"] == "target"


def test_uk_does_not_consolidate(monkeypatch):
    result = run([
        bill("Acme Ltd", 100, ref="INV-1"),
        bill("Acme Ltd", 250, ref="INV-2"),
    ], monkeypatch)
    assert len(result.rows) == 2
    assert result.rows[0]["amount_currency"] == "source"


# ---------------------------------------------------------------------------
# Bank file
# ---------------------------------------------------------------------------

def test_finalise_writes_columns_in_template_order(monkeypatch):
    result = run([bill("Acme Ltd", 100)], monkeypatch)
    columns = ["name", "recipientEmail", "paymentReference", "receiverType",
               "amountCurrency", "amount", "sourceCurrency", "targetCurrency",
               "sortCode", "accountNumber"]
    final = wp.finalise({"region": "UK"}, result.rows, make_ctx(), columns)

    text = final.artifact_bytes.decode("utf-8-sig")
    header, row = text.splitlines()[:2]
    assert header == ",".join(columns)
    assert row.split(",") == [
        "Acme Ltd", "acme@x.com", "INV-1", "Business",
        "source", "100.0", "GBP", "GBP", "20-00-00", "12345678",
    ]
    assert final.status is RunStatus.COMPLETE


def test_unknown_template_column_is_blank_not_shifted(monkeypatch):
    result = run([bill("Acme Ltd", 100)], monkeypatch)
    final = wp.finalise({"region": "UK"}, result.rows, make_ctx(),
                        ["name", "mysteryColumn", "amount"])
    row = final.artifact_bytes.decode("utf-8-sig").splitlines()[1]
    assert row.split(",") == ["Acme Ltd", "", "100.0"]
