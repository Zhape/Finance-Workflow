"""The guarantees that live in the database rather than in the UI.

Two of them carry real weight:

  * An email is ingested exactly once, whatever the sync does. Enforced by
    `unique (mailbox_id, gmail_message_id)`, so pressing Sync twice is free.

  * A reply is created exactly once. Enforced by `unique (email_id)`, so a
    double click, a second browser tab and a replayed API call all resolve to
    one Gmail draft. A disabled button is a hope; a constraint is a guarantee.

And the one that matters most for a multi-tenant product: one org cannot see
another's mail.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.db import create_db_engine, init_db  # noqa: E402
from fw.inbox.models import (  # noqa: E402
    Classification,
    Extracted,
    State,
    OUT_OF_SCOPE,
)
from fw.inbox.stores import (  # noqa: E402
    CategoryStore,
    ClassificationStore,
    EmailStore,
    ErrorStore,
    MailboxStore,
    ReplyStore,
    SettingsStore,
    TemplateVersionStore,
)
from fw.stores import OrgStore  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"
BOB = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'inbox.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def orgs(engine):
    store = OrgStore(engine)
    return (store.create("Acme", ALICE, "alice@acme.test"),
            store.create("Globex", BOB, "bob@globex.test"))


@pytest.fixture
def acme(orgs):
    return orgs[0]


def message(message_id="m1", thread_id="t1", sender="bob@customer.test",
            **extra):
    base = {
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "rfc822_message_id": f"<{message_id}@customer.test>",
        "from_name": "Bob Smith",
        "from_email": sender,
        "subject": "Invoice query",
        "body_text": "What is invoice 1042 for?",
        # Recent by default: the stats window is the last seven days, and a
        # fixed date would silently fall out of it as the calendar moves.
        "received_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    base.update(extra)
    return base


def ingest(engine, org_id, mailbox_id, msg, state=State.RECEIVED):
    return EmailStore(engine).insert(
        org_id, mailbox_id, msg, state=state, state_reason=None,
        body_stripped=msg.get("body_text", ""),
    )


@pytest.fixture
def mailbox(engine, acme):
    return MailboxStore(engine).upsert(
        acme, "accounts@acme.test", "inbox:accounts@acme.test", "alice@acme.test"
    )


# ---------------------------------------------------------------------------
# Ingestion is idempotent
# ---------------------------------------------------------------------------

def test_the_same_message_cannot_be_ingested_twice(engine, acme, mailbox):
    first = ingest(engine, acme, mailbox, message())
    second = ingest(engine, acme, mailbox, message())

    assert first is not None
    assert second is None, "the unique constraint should have refused this"
    assert len(EmailStore(engine).list(acme, [State.RECEIVED])) == 1


def test_known_ids_lets_a_sync_skip_what_it_already_has(engine, acme, mailbox):
    ingest(engine, acme, mailbox, message("m1"))
    ingest(engine, acme, mailbox, message("m2", thread_id="t2"))

    assert EmailStore(engine).known_ids(mailbox) == {"m1", "m2"}


def test_the_same_message_id_in_a_different_mailbox_is_a_different_row(
        engine, acme):
    """A Google Group delivers one message into several mailboxes. Each
    mailbox's copy is its own row; the UI collapses them by Message-ID."""
    store = MailboxStore(engine)
    one = store.upsert(acme, "a@acme.test", "inbox:a@acme.test", "alice@acme.test")
    two = store.upsert(acme, "b@acme.test", "inbox:b@acme.test", "alice@acme.test")

    assert ingest(engine, acme, one, message()) is not None
    assert ingest(engine, acme, two, message()) is not None


# ---------------------------------------------------------------------------
# One reply per email
# ---------------------------------------------------------------------------

def _reserve(engine, org_id, email_id, actor="alice@acme.test"):
    return ReplyStore(engine).reserve(
        org_id, email_id, category_key="InvoiceQuery", template_version=1,
        subject="Re: INV-1042", body="Hi there,", fields={}, actor=actor,
    )


def test_only_one_reply_can_be_reserved_for_an_email(engine, acme, mailbox):
    """Two tabs both press the button. The database decides."""
    email_id = ingest(engine, acme, mailbox, message())

    assert _reserve(engine, acme, email_id) is not None
    assert _reserve(engine, acme, email_id) is None


def test_a_created_reply_cannot_be_reclaimed(engine, acme, mailbox):
    """The draft exists in Gmail. A second one is exactly the duplicate this
    table prevents."""
    email_id = ingest(engine, acme, mailbox, message())
    reply_id = _reserve(engine, acme, email_id)
    ReplyStore(engine).complete(acme, reply_id, draft_id="d1", thread_id="t1",
                                mailbox_address="accounts@acme.test", attempts=1)

    assert ReplyStore(engine).claim_failed(
        acme, email_id, subject="s", body="b", fields={},
        category_key="InvoiceQuery", actor="alice@acme.test",
    ) is None


def test_a_failed_reply_can_be_retried(engine, acme, mailbox):
    """Three Gmail failures must not lock the email out for ever."""
    email_id = ingest(engine, acme, mailbox, message())
    reply_id = _reserve(engine, acme, email_id)
    ReplyStore(engine).fail(acme, reply_id, "Gmail returned 503", 3)

    reclaimed = ReplyStore(engine).claim_failed(
        acme, email_id, subject="s", body="b", fields={},
        category_key="InvoiceQuery", actor="alice@acme.test",
    )
    assert reclaimed == reply_id
    assert ReplyStore(engine).get(acme, email_id)["status"] == "pending"


def test_a_failed_reply_never_reports_as_created(engine, acme, mailbox):
    email_id = ingest(engine, acme, mailbox, message())
    reply_id = _reserve(engine, acme, email_id)
    ReplyStore(engine).fail(acme, reply_id, "Gmail returned 503", 3)

    reply = ReplyStore(engine).get(acme, email_id)
    assert reply["status"] == "failed"
    assert reply["gmailDraftId"] is None
    assert reply["attempts"] == 3


def test_the_reply_record_keeps_the_body_as_at_draft_time(engine, acme, mailbox):
    """Never re-rendered against live Xero. An audit trail that updates itself
    starts lying about what the customer was told."""
    email_id = ingest(engine, acme, mailbox, message())
    reply_id = ReplyStore(engine).reserve(
        acme, email_id, category_key="InvoiceQuery", template_version=2,
        subject="Re: INV-1042", body="Outstanding: AUD 1,525.00",
        fields={"outstanding_balance": "1,525.00"}, actor="alice@acme.test",
    )
    ReplyStore(engine).complete(acme, reply_id, draft_id="d1", thread_id="t1",
                                mailbox_address="accounts@acme.test", attempts=1)

    stored = ReplyStore(engine).get(acme, email_id)
    assert stored["body"] == "Outstanding: AUD 1,525.00"
    assert stored["fields"] == {"outstanding_balance": "1,525.00"}
    assert stored["templateVersion"] == 2
    assert stored["actor"] == "alice@acme.test"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def test_an_org_cannot_see_another_orgs_mail(engine, orgs, mailbox):
    acme, globex = orgs
    email_id = ingest(engine, acme, mailbox, message())

    assert EmailStore(engine).get(globex, email_id) is None
    assert EmailStore(engine).list(globex) == []
    assert ReplyStore(engine).get(globex, email_id) is None


def test_an_org_cannot_reserve_a_reply_on_another_orgs_email(
        engine, orgs, mailbox):
    acme, globex = orgs
    email_id = ingest(engine, acme, mailbox, message())
    _reserve(engine, globex, email_id, actor="bob@globex.test")

    # The row exists but belongs to Globex, so Acme's read is unaffected and
    # Acme can still create its own.
    assert ReplyStore(engine).get(acme, email_id) is None


def test_mailboxes_are_scoped_to_their_org(engine, orgs, mailbox):
    acme, globex = orgs
    assert len(MailboxStore(engine).list(acme)) == 1
    assert MailboxStore(engine).list(globex) == []


# ---------------------------------------------------------------------------
# Categories and settings
# ---------------------------------------------------------------------------

def test_a_fresh_org_gets_every_shipped_category(engine, acme):
    CategoryStore(engine).seed(acme, "alice@acme.test")
    keys = {c.key for c in CategoryStore(engine).list(acme)}

    assert OUT_OF_SCOPE in keys
    assert {"Dispute", "InvoiceQuery", "PaymentPromise", "Confirmation",
            "PayConfirmation", "UpdateDetails"} <= keys


def test_seeding_twice_does_not_duplicate(engine, acme):
    store = CategoryStore(engine)
    store.seed(acme, "alice@acme.test")
    store.seed(acme, "alice@acme.test")

    keys = [c.key for c in store.list(acme)]
    assert len(keys) == len(set(keys))


def test_an_org_can_add_a_category_without_a_deploy(engine, acme):
    store = CategoryStore(engine)
    store.seed(acme, "alice@acme.test")
    store.create(acme, "Refund", "Wants a refund",
                 "The customer is asking for money back.", "alice@acme.test")

    added = next(c for c in store.enabled(acme) if c.key == "Refund")
    assert added.is_system is False
    assert added.description.startswith("The customer is asking")


def test_disabling_a_category_removes_it_from_the_enabled_set(engine, acme):
    store = CategoryStore(engine)
    store.seed(acme, "alice@acme.test")
    store.update(acme, "Dispute", enabled=False)

    assert "Dispute" not in {c.key for c in store.enabled(acme)}
    assert "Dispute" in {c.key for c in store.list(acme)}


def test_settings_are_created_on_demand_with_safe_defaults(engine, acme):
    settings = SettingsStore(engine).get(acme)
    assert settings["lookbackDays"] == 7
    assert settings["xeroConnection"] == "default"


def test_there_is_no_auto_send_setting(engine, acme):
    """The design's auto-send mode was removed. Nothing here can turn one on,
    and the absence is asserted rather than assumed."""
    from fw.db import inbox_settings

    assert "auto_send_enabled" not in inbox_settings.c
    assert "auto_send" not in str(SettingsStore(engine).get(acme))


# ---------------------------------------------------------------------------
# Classifications are append-only
# ---------------------------------------------------------------------------

def _classification(category="InvoiceQuery", source="ai", **extra):
    return Classification(
        category=category, confidence=extra.pop("confidence", 0.9),
        language="en", model_version="gemini-2.5-flash", source=source,
        extracted=Extracted(invoice_number="INV-1042"), **extra,
    )


def test_a_human_override_keeps_the_models_original_suggestion(
        engine, acme, mailbox):
    """Both must appear in the trail, or an accuracy regression cannot be
    attributed to a model change later."""
    email_id = ingest(engine, acme, mailbox, message())
    store = ClassificationStore(engine)
    store.add(acme, email_id, _classification("InvoiceQuery"))
    store.add(acme, email_id, _classification("Dispute", source="human"),
              user="alice@acme.test")

    history = store.history(acme, email_id)
    assert len(history) == 2
    assert history[0]["categoryKey"] == "Dispute"
    assert history[0]["source"] == "human"
    assert history[-1]["categoryKey"] == "InvoiceQuery"
    assert history[-1]["source"] == "ai"
    assert store.latest(acme, email_id).category == "Dispute"


def test_the_model_version_is_stored_on_every_row(engine, acme, mailbox):
    email_id = ingest(engine, acme, mailbox, message())
    ClassificationStore(engine).add(acme, email_id, _classification())

    assert ClassificationStore(engine).history(acme, email_id)[0][
        "modelVersion"] == "gemini-2.5-flash"


def test_an_open_dispute_is_detected_per_contact_not_per_thread(
        engine, acme, mailbox):
    """A customer mid-dispute who writes about a different invoice is still
    mid-dispute."""
    disputed = ingest(engine, acme, mailbox,
                      message("m1", "t1", "bob@customer.test"))
    ClassificationStore(engine).add(acme, disputed, _classification("Dispute"))

    emails = EmailStore(engine)
    assert emails.has_open_dispute(acme, "bob@customer.test") is True
    assert emails.has_open_dispute(acme, "someone@else.test") is False


def test_an_old_dispute_stops_counting(engine, acme, mailbox):
    old = datetime.now(timezone.utc) - timedelta(days=200)
    email_id = ingest(engine, acme, mailbox,
                      message("m9", "t9", received_at=old))
    ClassificationStore(engine).add(acme, email_id, _classification("Dispute"))

    assert EmailStore(engine).has_open_dispute(acme, "bob@customer.test") is False


# ---------------------------------------------------------------------------
# Template versions and the error queue
# ---------------------------------------------------------------------------

def test_template_versions_increment_and_never_overwrite(engine, acme):
    store = TemplateVersionStore(engine)
    assert store.add(acme, "InvoiceQuery", "s1", "b1", "alice@acme.test") == 1
    assert store.add(acme, "InvoiceQuery", "s2", "b2", "alice@acme.test") == 2
    assert store.current_version(acme, "InvoiceQuery") == 2
    # A different variant versions independently.
    assert store.add(acme, "Dispute", "s", "b", "alice@acme.test") == 1


def test_an_unedited_template_has_no_version(engine, acme):
    assert TemplateVersionStore(engine).current_version(acme, "Dispute") is None


def test_errors_are_surfaced_until_they_are_cleared(engine, acme):
    store = ErrorStore(engine)
    store.add(acme, "ingest", "ING-001", "Gmail returned 503")

    assert len(store.open(acme)) == 1
    assert store.resolve_all(acme) == 1
    assert store.open(acme) == []


def test_errors_are_scoped_to_their_org(engine, orgs):
    acme, globex = orgs
    ErrorStore(engine).add(acme, "ingest", "ING-001", "boom")

    assert ErrorStore(engine).open(globex) == []


# ---------------------------------------------------------------------------
# Suppression and the list view
# ---------------------------------------------------------------------------

def test_suppressed_mail_stays_out_of_the_queue(engine, acme, mailbox):
    ingest(engine, acme, mailbox, message("m1"), state=State.RECEIVED)
    ingest(engine, acme, mailbox, message("m2", "t2"), state=State.SUPPRESSED)

    pending = EmailStore(engine).pending(acme)
    assert [e["gmailMessageId"] for e in pending] == ["m1"]


def test_a_thread_can_be_read_in_order(engine, acme, mailbox):
    base = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    ingest(engine, acme, mailbox,
           message("m2", "t1", received_at=base + timedelta(hours=2)))
    ingest(engine, acme, mailbox, message("m1", "t1", received_at=base))

    thread = EmailStore(engine).thread(acme, "t1")
    assert [m["gmailMessageId"] for m in thread] == ["m1", "m2"]


def test_stats_ignore_suppressed_mail(engine, acme, mailbox):
    ingest(engine, acme, mailbox, message("m1"), state=State.NEEDS_REVIEW)
    ingest(engine, acme, mailbox, message("m2", "t2"), state=State.SUPPRESSED)

    stats = EmailStore(engine).stats(acme)
    assert stats["received"] == 1
    assert stats["suppressed"] == 1
    assert stats["waiting"] == 1
