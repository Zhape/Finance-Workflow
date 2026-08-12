"""Overdue chase-up rules.

The judgement in this workflow is the grouping and the ageing, so that is what
is pinned down here. No network, no email.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.contract import RunContext, RunStatus  # noqa: E402
from fw.workflows import overdue_chaseup as oc  # noqa: E402


class FakeCreds:
    def xero(self, connection: str):
        return ("token", "tenant")


class FakeXero:
    def __init__(self, invoices):
        self._invoices = invoices

    def __call__(self, *_a, **_k):
        return self

    def get_receivables(self):
        return self._invoices


def invoice(customer, amount, days_overdue, number="INV-1",
            email="pat@customer.test", currency="GBP", contact_id=None):
    due = date.today() - timedelta(days=days_overdue)
    return {
        "InvoiceID": f"{customer}-{number}",
        "InvoiceNumber": number,
        "Type": "ACCREC",
        "CurrencyCode": currency,
        "AmountDue": amount,
        "DueDateString": f"{due.isoformat()}T00:00:00",
        "Contact": {
            "ContactID": contact_id or customer,
            "Name": customer,
            "EmailAddress": email,
        },
    }


def run(invoices, monkeypatch, **params):
    monkeypatch.setattr(oc, "XeroClient", FakeXero(invoices))
    ctx = RunContext(org_id="t", creds=FakeCreds(), log=lambda m: None)
    return oc.run({"connection": "default", "min_days_overdue": "1", **params}, ctx)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_one_row_per_customer_not_per_invoice(monkeypatch):
    """Four invoices to one customer is one chase, not four."""
    result = run([
        invoice("Acme Ltd", 100, 40, "INV-1"),
        invoice("Acme Ltd", 250, 10, "INV-2"),
        invoice("Acme Ltd", 50, 5, "INV-3"),
    ], monkeypatch)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["amount"] == 400
    assert row["count"] == 3
    assert row["oldest_days"] == 40
    assert row["references"] == "INV-1, INV-2, INV-3"


def test_settled_invoices_are_excluded(monkeypatch):
    result = run([
        invoice("Acme Ltd", 100, 20),
        invoice("Paid Ltd", 0, 30, "INV-9"),
    ], monkeypatch)
    assert [r["name"] for r in result.rows] == ["Acme Ltd"]


def test_invoices_inside_the_grace_period_are_excluded(monkeypatch):
    result = run([
        invoice("Late Ltd", 100, 30),
        invoice("Fresh Ltd", 100, 2, "INV-2"),
    ], monkeypatch, min_days_overdue="7")
    assert [r["name"] for r in result.rows] == ["Late Ltd"]


def test_small_balances_can_be_ignored(monkeypatch):
    result = run([
        invoice("Big Ltd", 500, 20),
        invoice("Small Ltd", 9, 20, "INV-2"),
    ], monkeypatch, min_amount="10")
    assert [r["name"] for r in result.rows] == ["Big Ltd"]


def test_nothing_overdue_is_empty_not_an_error(monkeypatch):
    result = run([invoice("Acme Ltd", 100, 0)], monkeypatch, min_days_overdue="7")
    assert result.status is RunStatus.EMPTY


# ---------------------------------------------------------------------------
# Ageing and tone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days,bucket,tone", [
    (5, "1-29 days", "gentle"),
    (29, "1-29 days", "gentle"),
    (30, "30-59 days", "reminder"),
    (60, "60-89 days", "firm"),
    (120, "90+ days", "final"),
])
def test_ageing_buckets(days, bucket, tone, monkeypatch):
    result = run([invoice("Acme Ltd", 100, days)], monkeypatch)
    assert result.rows[0]["bucket"] == bucket
    assert result.rows[0]["tone"] == tone


def test_the_message_escalates_with_lateness(monkeypatch):
    gentle = run([invoice("A Ltd", 100, 5)], monkeypatch).rows[0]["message"]
    final = run([invoice("A Ltd", 100, 120)], monkeypatch).rows[0]["message"]
    assert "Hope you're well" in gentle
    assert "urgent" in final.lower()
    assert "100.00" in final and "120" in final


def test_worst_debts_come_first(monkeypatch):
    result = run([
        invoice("Recent Ltd", 5000, 10, "INV-1"),
        invoice("Ancient Ltd", 100, 200, "INV-2"),
    ], monkeypatch)
    assert [r["name"] for r in result.rows] == ["Ancient Ltd", "Recent Ltd"]


# ---------------------------------------------------------------------------
# Contactability
# ---------------------------------------------------------------------------

def test_customers_without_an_email_are_flagged_not_dropped(monkeypatch):
    result = run([
        invoice("Acme Ltd", 100, 20),
        invoice("No Email Ltd", 200, 20, "INV-2", email=""),
    ], monkeypatch)

    names = {r["name"]: r for r in result.rows}
    assert names["No Email Ltd"]["contactable"] is False
    assert "No Email Ltd" in result.warnings[0]


def test_greeting_uses_a_first_name_for_people_and_the_whole_name_for_companies():
    assert oc.first_name("Jane Smith") == "Jane"
    assert oc.first_name("Acme Trading Company Ltd") == "Acme Trading Company Ltd"
    assert oc.first_name("") == "there"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def test_finalise_writes_a_chase_list(monkeypatch):
    result = run([invoice("Acme Ltd", 100, 45)], monkeypatch)
    ctx = RunContext(org_id="t", creds=None, log=lambda m: None)
    final = oc.finalise({}, result.rows, ctx, [])

    text = final.artifact_bytes.decode("utf-8-sig")
    header, row = text.splitlines()[:2]
    assert header.startswith("customer,email,currency,amount_owed")
    assert "Acme Ltd" in row
    assert final.status is RunStatus.COMPLETE


def test_without_gmail_nothing_is_drafted_and_it_says_so(monkeypatch):
    result = run([invoice("Acme Ltd", 100, 45)], monkeypatch)
    ctx = RunContext(org_id="t", creds=None, mailer=None, log=lambda m: None)
    final = oc.finalise({}, result.rows, ctx, [])
    assert "not connected" in final.summary
    assert final.artifact_bytes  # the file is produced regardless


class FakeMailer:
    """Stands in for GmailDrafter. Records drafts; cannot send, same as the
    real thing."""

    address = "finance@company.test"

    def __init__(self, fail_for: set[str] | None = None):
        self.drafts: list[tuple[str, str, str]] = []
        self._fail_for = fail_for or set()

    def create_draft(self, to, subject, body):
        if to in self._fail_for:
            raise RuntimeError("mailbox rejected it")
        self.drafts.append((to, subject, body))
        return f"draft-{len(self.drafts)}"


def test_with_gmail_one_draft_per_customer(monkeypatch):
    result = run([
        invoice("Acme Ltd", 100, 45, "INV-1", email="ap@acme.test"),
        invoice("Beta Ltd", 200, 95, "INV-2", email="ap@beta.test"),
    ], monkeypatch)
    mailer = FakeMailer()
    ctx = RunContext(org_id="t", creds=None, mailer=mailer, log=lambda m: None)
    final = oc.finalise({}, result.rows, ctx, [])

    assert len(mailer.drafts) == 2
    assert {d[0] for d in mailer.drafts} == {"ap@acme.test", "ap@beta.test"}
    assert "2 draft(s) created" in final.summary
    assert "finance@company.test" in final.summary
    assert "Nothing has been sent" in final.summary


def test_customers_without_an_email_are_skipped_not_failed(monkeypatch):
    result = run([
        invoice("Acme Ltd", 100, 45, "INV-1", email="ap@acme.test"),
        invoice("No Email Ltd", 200, 45, "INV-2", email=""),
    ], monkeypatch)
    mailer = FakeMailer()
    ctx = RunContext(org_id="t", creds=None, mailer=mailer, log=lambda m: None)
    final = oc.finalise({}, result.rows, ctx, [])

    assert len(mailer.drafts) == 1
    assert not final.warnings


def test_one_failed_draft_does_not_lose_the_rest(monkeypatch):
    """A mailbox rejecting one address must not cost the other twenty."""
    result = run([
        invoice("Good Ltd", 100, 45, "INV-1", email="ok@good.test"),
        invoice("Bad Ltd", 200, 45, "INV-2", email="bad@bad.test"),
    ], monkeypatch)
    mailer = FakeMailer(fail_for={"bad@bad.test"})
    ctx = RunContext(org_id="t", creds=None, mailer=mailer, log=lambda m: None)
    final = oc.finalise({}, result.rows, ctx, [])

    assert len(mailer.drafts) == 1
    assert final.status is RunStatus.COMPLETE
    assert "Bad Ltd" in final.warnings[0]
    assert final.artifact_bytes


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_org_templates_override_the_shipped_wording(monkeypatch):
    monkeypatch.setattr(oc, "XeroClient", FakeXero([invoice("Acme Ltd", 100, 45)]))
    ctx = RunContext(
        org_id="t", creds=FakeCreds(), log=lambda m: None,
        templates={"reminder": {"subject": "Your account, {customer}",
                                "body": "Pay {currency} {amount} please."}},
    )
    result = oc.run({"connection": "default", "min_days_overdue": "1"}, ctx)
    row = result.rows[0]
    assert row["subject"] == "Your account, Acme Ltd"
    assert row["message"] == "Pay GBP 100.00 please."


def test_a_typo_in_a_placeholder_shows_rather_than_crashing():
    out = oc.render("Hi {first}, you owe {nonsense}", {"first": "Sam"})
    assert out == "Hi Sam, you owe {nonsense}"


def test_shipped_defaults_cover_every_tone():
    defaults = oc.default_templates()
    assert set(defaults) == {t for _, _, t in oc.BUCKETS}
    for variant in defaults.values():
        assert variant["subject"] and variant["body"]
