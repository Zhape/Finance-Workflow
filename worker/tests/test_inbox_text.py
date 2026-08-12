"""Suppression and quote stripping — the two rules that run before the model.

Both are cheap, both are hard-coded, and both exist as much for cost as for
correctness. An out-of-office that reaches the classifier is money spent to
generate a reply that would start a loop; a sixty-message thread classified in
full is fifty-nine messages paid for twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.inbox.text import (  # noqa: E402
    display_name,
    html_to_text,
    strip_quoted,
    suppression_reason,
)

OURS = {"accounts@ourcompany.test"}


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def test_an_out_of_office_never_reaches_the_classifier():
    reason = suppression_reason(
        {"Auto-Submitted": "auto-replied"}, "bob@customer.test", OURS
    )
    assert reason and "Automatic reply" in reason


def test_auto_submitted_no_is_a_real_email():
    """'no' is the header's way of saying a human sent this."""
    assert suppression_reason(
        {"Auto-Submitted": "no"}, "bob@customer.test", OURS
    ) is None


def test_bulk_mail_is_suppressed():
    assert suppression_reason(
        {"Precedence": "bulk"}, "news@vendor.test", OURS
    ) is not None


def test_a_mailing_list_message_is_suppressed():
    assert suppression_reason(
        {"List-Unsubscribe": "<https://x.test/u>"}, "list@vendor.test", OURS
    ) is not None


def test_a_bounce_is_suppressed():
    assert suppression_reason({}, "MAILER-DAEMON@gmail.com", OURS) is not None


def test_our_own_mail_is_suppressed_so_replies_cannot_loop():
    reason = suppression_reason({}, "accounts@ourcompany.test", OURS)
    assert reason == "Sent from a connected mailbox"


def test_an_ordinary_customer_email_passes():
    assert suppression_reason(
        {"Subject": "Invoice query"}, "bob@customer.test", OURS
    ) is None


# ---------------------------------------------------------------------------
# Quote stripping
# ---------------------------------------------------------------------------

def test_a_reply_is_reduced_to_its_new_words():
    body = (
        "Hi, what is invoice 1042 for?\n"
        "\n"
        "On Tue, 4 Aug 2026 at 09:12, Accounts <accounts@ourcompany.test> wrote:\n"
        "> Your invoice is attached.\n"
        "> Kind regards\n"
    )
    assert strip_quoted(body) == "Hi, what is invoice 1042 for?"


def test_outlook_style_history_is_stripped():
    body = (
        "Please see attached remittance.\n"
        "\n"
        "-----Original Message-----\n"
        "From: Accounts\n"
        "Sent: 01 August 2026\n"
    )
    assert strip_quoted(body) == "Please see attached remittance."


def test_a_from_block_starts_the_history():
    body = "We'll pay on Friday.\n\nFrom: Accounts <a@b.test>\nSubject: chase\n"
    assert strip_quoted(body) == "We'll pay on Friday."


def test_a_signature_is_cut():
    assert strip_quoted("Paid yesterday.\n\n--\nBob Smith\nCFO") == \
        "Paid yesterday."


def test_stripping_never_returns_nothing():
    """A blank classifier input guarantees a wrong answer, so the original
    survives when the heuristic would eat everything."""
    body = "> Only quoted content here"
    assert strip_quoted(body) == body


def test_a_message_with_no_history_is_untouched():
    body = "Could you resend invoice 1042?"
    assert strip_quoted(body) == body


# ---------------------------------------------------------------------------
# HTML-only mail
# ---------------------------------------------------------------------------

def test_html_quoted_history_is_dropped_before_the_tags_are():
    html = (
        "<div>Thanks, that's clear now.</div>"
        "<blockquote>Your invoice is attached, see below for details.</blockquote>"
    )
    assert html_to_text(html) == "Thanks, that's clear now."


def test_html_entities_are_decoded():
    assert html_to_text("<p>Fish &amp; Chips Ltd</p>") == "Fish & Chips Ltd"


# ---------------------------------------------------------------------------
# From headers
# ---------------------------------------------------------------------------

def test_a_from_header_splits_into_name_and_address():
    assert display_name('"Bob Smith" <Bob@Customer.test>') == \
        ("Bob Smith", "bob@customer.test")


def test_a_bare_address_has_no_name():
    assert display_name("bob@customer.test") == (None, "bob@customer.test")
