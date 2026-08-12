"""One email from Gmail to a finished draft, with everything external faked.

The order the pipeline runs in is the product's safety argument, so it is
asserted rather than assumed: suppression before the model is called, the Xero
lookup before anything is rendered, and rendering that produces approved
wording even when every external service is down.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.db import create_db_engine, init_db  # noqa: E402
from fw.inbox import templates as tmpl  # noqa: E402
from fw.inbox.classify import ClassificationError  # noqa: E402
from fw.inbox.models import OUT_OF_SCOPE, Outcome, State  # noqa: E402
from fw.inbox.pipeline import Pipeline, SyncReport  # noqa: E402
from fw.inbox.stores import (  # noqa: E402
    CategoryStore,
    ClassificationStore,
    DraftStore,
    EmailStore,
    ErrorStore,
    LookupStore,
    MailboxStore,
    ReplyStore,
    SettingsStore,
    TemplateVersionStore,
)
from fw.stores import OrgStore  # noqa: E402
from fw.xero import XeroError  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGmail:
    """Stands in for a connected mailbox."""

    MAX_MESSAGES_PER_SYNC = 40

    def __init__(self, messages):
        self._messages = {m["gmail_message_id"]: m for m in messages}
        self.address = "accounts@acme.test"
        self.fetched: list[str] = []

    def list_recent(self, lookback_days=7, limit=40, known=None):
        """Mirrors the real signature: filters known ids, reports more waiting.

        The filtering is the point. A fake that listed everything and let the
        caller filter would hide the bug this shape exists to prevent — a sync
        that can never reach mail older than its first page.
        """
        known = known or set()
        fresh = [mid for mid in self._messages if mid not in known]
        return fresh[:limit], len(fresh) > limit

    def fetch(self, message_id):
        self.fetched.append(message_id)
        return dict(self._messages[message_id])


class FakeClassifier:
    model_version = "fake-model-1"

    def __init__(self, answer=None, error=None):
        self._answer = answer
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def classify(self, subject, body, categories):
        self.calls.append((subject, body))
        if self._error:
            raise self._error
        return dict(self._answer)


class FakeXero:
    def __init__(self, invoices=None, raises=None):
        self._invoices = invoices or []
        self._raises = raises

    def get_invoices_by_number(self, number):
        if self._raises:
            raise self._raises
        return [i for i in self._invoices
                if i["InvoiceNumber"].lower() == number.strip().lower()]

    def find_contacts_by_email(self, email):
        return []

    def find_contacts_by_domain(self, domain):
        return []

    def get_open_invoices_for_contact(self, contact_id):
        return []


INVOICE = {
    "InvoiceID": "x1",
    "InvoiceNumber": "INV-1042",
    "Contact": {"ContactID": "c1", "Name": "Northwind Trading"},
    "Total": 3050.0,
    "AmountDue": 1525.0,
    "CurrencyCode": "AUD",
    "DueDateString": "2026-07-31T00:00:00",
    "Status": "AUTHORISED",
    "LineItems": [{"Description": "Platform subscription, June 2026"}],
}

GOOD_ANSWER = {
    "category": "InvoiceQuery",
    "confidence": 0.94,
    "secondary_category": "None",
    "secondary_confidence": 0.0,
    "language": "en",
    "invoice_number": "INV-1042",
    "invoice_number_confidence": 0.96,
}


def gmail_message(message_id="m1", headers=None, body=None, sender=None):
    return {
        "gmail_message_id": message_id,
        "gmail_thread_id": "t1",
        "rfc822_message_id": f"<{message_id}@customer.test>",
        "in_reply_to": None,
        "email_references": None,
        "from": sender or "Bob Smith <bob@customer.test>",
        "subject": "Query on INV-1042",
        "body_text": body if body is not None else "What is INV-1042 for?",
        "body_html": "",
        "snippet": "What is INV-1042 for?",
        "has_attachments": False,
        "received_at": datetime.now(timezone.utc),
        "headers": headers or {"From": "Bob Smith <bob@customer.test>"},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def org(engine):
    org_id = OrgStore(engine).create("Acme", ALICE, "alice@acme.test")
    CategoryStore(engine).seed(org_id, "alice@acme.test")
    SettingsStore(engine).get(org_id)
    return org_id


@pytest.fixture
def stores(engine):
    return {
        "categories": CategoryStore(engine), "emails": EmailStore(engine),
        "classifications": ClassificationStore(engine),
        "lookups": LookupStore(engine), "drafts": DraftStore(engine),
        "replies": ReplyStore(engine), "versions": TemplateVersionStore(engine),
        "errors": ErrorStore(engine),
    }


@pytest.fixture
def mailbox(engine, org):
    mailbox_id = MailboxStore(engine).upsert(
        org, "accounts@acme.test", "inbox:accounts@acme.test", "alice@acme.test"
    )
    return {"id": mailbox_id, "address": "accounts@acme.test",
            "connectionName": "inbox:accounts@acme.test"}


def build(org, stores, classifier=None, xero=None):
    return Pipeline(
        org_id=org, stores=stores,
        classification_client=classifier,
        xero=xero, xero_tenant_id="tenant-abc" if xero else None,
        sender_name="Peter Zha",
        template_source=tmpl.default_for,
        log=lambda m: None,
    )


def run_sync(pipeline, mailbox, gmail, own=None):
    report = SyncReport()
    pipeline.ingest_mailbox(mailbox, gmail, 7, own or {"accounts@acme.test"},
                            report)
    pipeline.triage_pending(report)
    return report


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_an_email_becomes_a_filled_draft(engine, org, stores, mailbox):
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER),
                     FakeXero([INVOICE]))
    report = run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    assert report.ingested == 1
    assert report.triaged == 1

    email = stores["emails"].list(org)[0]
    assert email["state"] == State.NEEDS_REVIEW

    draft = stores["drafts"].latest(org, email["id"])
    assert draft["categoryKey"] == "InvoiceQuery"
    assert draft["missingSlots"] == []
    assert draft["blockers"] == []
    # Values come from Xero, and the balance is the figure quoted.
    assert "INV-1042" in draft["body"]
    assert "AUD" in draft["body"]
    assert "1,525.00" in draft["body"]


def test_the_draft_is_the_approved_template_with_values_dropped_in(
        engine, org, stores, mailbox):
    """The same guarantee as the render tests, asserted after a full run."""
    from fw.inbox import render

    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER),
                     FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    draft = stores["drafts"].latest(org, email["id"])

    assert render.unfill(draft["body"], draft["fields"]) == \
        tmpl.DEFAULT_BODIES["InvoiceQuery"]


def test_the_lookup_outcome_is_recorded_on_every_email(
        engine, org, stores, mailbox):
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER),
                     FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    assert stores["lookups"].latest(org, email["id"])["outcome"] == Outcome.FOUND


def test_a_backlog_is_triaged_in_bounded_batches(engine, org, stores, mailbox):
    """A hundred model calls in one request is a timeout, not a feature.

    Re-triaging after a classifier outage produces exactly that backlog, so a
    sync classifies a bounded batch, says how many are left, and drains the
    rest on the next press.
    """
    from fw.inbox.pipeline import MAX_TRIAGE_PER_SYNC

    total = MAX_TRIAGE_PER_SYNC + 7
    messages = [dict(gmail_message(f"m{n}"), gmail_thread_id=f"t{n}")
                for n in range(total)]
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER), FakeXero([INVOICE]))

    first = run_sync(pipeline, mailbox, FakeGmail(messages))
    assert first.ingested == total
    assert first.triaged == MAX_TRIAGE_PER_SYNC
    assert first.untriaged == 7

    second = run_sync(pipeline, mailbox, FakeGmail(messages))
    assert second.triaged == 7
    assert second.untriaged == 0
    assert stores["emails"].pending(org) == []


def test_repeated_syncs_reach_older_mail(engine, org, stores, mailbox):
    """A regression test for the bug that lost four days of a real inbox.

    Gmail lists newest first. The original code took the first `limit` ids and
    only then discarded ones already stored, so every sync after the first
    fetched the same newest page, found it all known, and ingested nothing —
    the cap was a permanent ceiling rather than a page size. Anything older was
    unreachable no matter how many times the button was pressed.

    Filtering inside the listing makes each sync walk further back, so the
    whole lookback window is eventually read.
    """
    total = 95
    messages = [
        dict(gmail_message(f"m{n}"), gmail_thread_id=f"t{n}")
        for n in range(total)
    ]
    gmail = FakeGmail(messages)
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER), FakeXero([INVOICE]))

    first = run_sync(pipeline, mailbox, gmail)
    assert first.ingested == 40
    assert first.more_waiting is True, "should admit more is waiting"

    second = run_sync(pipeline, mailbox, gmail)
    assert second.ingested == 40, "the second sync must reach new mail"

    third = run_sync(pipeline, mailbox, gmail)
    assert third.ingested == total - 80
    assert third.more_waiting is False, "the window is now fully read"

    stored = stores["emails"].list(org, limit=500)
    assert len(stored) == total
    # Every message fetched exactly once across all three syncs.
    assert len(gmail.fetched) == total
    assert len(set(gmail.fetched)) == total


def test_syncing_twice_changes_nothing(engine, org, stores, mailbox):
    gmail = FakeGmail([gmail_message()])
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER),
                     FakeXero([INVOICE]))

    run_sync(pipeline, mailbox, gmail)
    second = run_sync(pipeline, mailbox, gmail)

    assert second.ingested == 0
    assert len(stores["emails"].list(org)) == 1
    assert len(gmail.fetched) == 1, "the second sync refetched a known message"


# ---------------------------------------------------------------------------
# Suppression happens before the model is called
# ---------------------------------------------------------------------------

def test_an_auto_reply_never_reaches_the_classifier(
        engine, org, stores, mailbox):
    """The cheap rule that both prevents a reply loop and cuts model spend."""
    classifier = FakeClassifier(GOOD_ANSWER)
    pipeline = build(org, stores, classifier, FakeXero([INVOICE]))
    message = gmail_message(headers={"Auto-Submitted": "auto-replied",
                                     "From": "bob@customer.test"})

    report = run_sync(pipeline, mailbox, FakeGmail([message]))

    assert report.suppressed == 1
    assert report.ingested == 0
    assert classifier.calls == [], "an auto-reply was sent to the model"
    assert stores["emails"].list(org, [State.SUPPRESSED])[0]["stateReason"]


def test_our_own_mail_is_suppressed(engine, org, stores, mailbox):
    classifier = FakeClassifier(GOOD_ANSWER)
    pipeline = build(org, stores, classifier, FakeXero([INVOICE]))
    message = gmail_message(sender="Accounts <accounts@acme.test>",
                            headers={"From": "accounts@acme.test"})

    report = run_sync(pipeline, mailbox, FakeGmail([message]))
    assert report.suppressed == 1
    assert classifier.calls == []


def test_quoted_history_is_stripped_before_classification(
        engine, org, stores, mailbox):
    """A sixty-message thread must not be paid for on every reply."""
    body = (
        "What is INV-1042 for?\n\n"
        "On Tue, 4 Aug 2026, Accounts <accounts@acme.test> wrote:\n"
        "> Here is your statement, with forty lines of history below\n"
    )
    classifier = FakeClassifier(GOOD_ANSWER)
    pipeline = build(org, stores, classifier, FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message(body=body)]))

    _subject, classified_body = classifier.calls[0]
    assert classified_body == "What is INV-1042 for?"
    assert "statement" not in classified_body


# ---------------------------------------------------------------------------
# Degraded behaviour
# ---------------------------------------------------------------------------

def test_with_no_classifier_the_email_still_reaches_a_person(
        engine, org, stores, mailbox):
    """An outage costs triage speed, never correctness."""
    pipeline = build(org, stores, classifier=None, xero=FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    assert email["state"] == State.NEEDS_REVIEW

    classification = stores["classifications"].latest(org, email["id"])
    assert classification.category == OUT_OF_SCOPE
    assert classification.source == "fallback"
    # And a holding reply is still ready, from approved wording.
    assert stores["drafts"].latest(org, email["id"])["body"].startswith("Hi ")


def test_a_classifier_failure_is_recorded_in_the_error_queue(
        engine, org, stores, mailbox):
    pipeline = build(
        org, stores,
        FakeClassifier(error=ClassificationError("timed out", "CLS-001")),
        FakeXero([INVOICE]),
    )
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    codes = [e["code"] for e in stores["errors"].open(org)]
    assert "CLS-001" in codes


def test_an_off_schema_category_is_recorded_as_well_as_coerced(
        engine, org, stores, mailbox):
    answer = dict(GOOD_ANSWER, category="RefundRequest")
    pipeline = build(org, stores, FakeClassifier(answer), FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    assert stores["classifications"].latest(org, email["id"]).category == \
        OUT_OF_SCOPE
    assert "CLS-002" in [e["code"] for e in stores["errors"].open(org)]


def test_with_xero_unreachable_the_lookup_is_recorded_not_skipped(
        engine, org, stores, mailbox):
    """Never draft against what the customer typed because Xero was down."""
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER),
                     FakeXero(raises=XeroError("Xero timed out after 10s.")))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    found = stores["lookups"].latest(org, email["id"])
    assert found["outcome"] == Outcome.TIMED_OUT

    # The draft exists but cannot be sent: the invoice slots are empty.
    draft = stores["drafts"].latest(org, email["id"])
    assert "invoice_number" in draft["missingSlots"]
    assert [b["code"] for b in draft["blockers"]] == ["unfilled_slots"]


def test_with_no_xero_connection_the_lookup_is_an_error_not_a_success(
        engine, org, stores, mailbox):
    pipeline = build(org, stores, FakeClassifier(GOOD_ANSWER), xero=None)
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    assert stores["lookups"].latest(org, email["id"])["outcome"] == Outcome.ERROR


def test_one_bad_email_does_not_abandon_the_rest_of_the_sync(
        engine, org, stores, mailbox):
    class Exploding(FakeClassifier):
        def classify(self, subject, body, categories):
            if "boom" in subject:
                raise RuntimeError("unexpected")
            return dict(GOOD_ANSWER)

    messages = [
        gmail_message("m1"),
        dict(gmail_message("m2"), subject="boom", gmail_thread_id="t2"),
        dict(gmail_message("m3"), gmail_thread_id="t3"),
    ]
    pipeline = build(org, stores, Exploding(GOOD_ANSWER), FakeXero([INVOICE]))
    report = run_sync(pipeline, mailbox, FakeGmail(messages))

    assert report.ingested == 3
    assert report.triaged == 2
    assert report.failed == 1


# ---------------------------------------------------------------------------
# Multi-intent
# ---------------------------------------------------------------------------

def test_a_two_intent_email_is_flagged_for_a_person(
        engine, org, stores, mailbox):
    """'I've paid 1042, and I'm disputing 1043.'"""
    answer = dict(GOOD_ANSWER, category="Confirmation",
                  secondary_category="Dispute", secondary_confidence=0.62)
    pipeline = build(org, stores, FakeClassifier(answer), FakeXero([INVOICE]))
    run_sync(pipeline, mailbox, FakeGmail([gmail_message()]))

    email = stores["emails"].list(org)[0]
    classification = stores["classifications"].latest(org, email["id"])
    assert classification.multi_intent is True

    from fw.inbox import review

    flags = review.flags(classification, stores["lookups"].latest(
        org, email["id"]), False, False)
    assert "multi_intent" in [f["code"] for f in flags]
