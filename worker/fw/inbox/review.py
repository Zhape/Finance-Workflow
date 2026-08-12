"""What a person needs to know before they press send, and what stops them.

The original design had this as an auto-send gate: nine checks, all of which
had to pass before the product would answer a customer unattended. There is no
auto-send now — every reply is a Gmail draft that a human sends — so the same
checks do a different job, and the difference matters:

  * **Blockers** stop a draft being created at all. There are two, and both are
    about the draft being incomplete rather than about judgement. They are
    enforced here *and* again in the API, because "the button was disabled" is
    not a guarantee, it is a hope about the client.

  * **Flags** are the rest. They no longer decide anything — they are shown in
    the review pane so the reason an email needs care is visible, rather than
    being something the reader has to reconstruct from the evidence.

Keeping the flags after removing auto-send is deliberate. They are the record
of *why* a case is unusual, and the day auto-send is added back they are
already the rule set it needs.
"""

from __future__ import annotations

from typing import Any

from .models import (
    CATEGORY_CONFIDENCE_FLOOR,
    Classification,
    Outcome,
)
from .render import unfilled

# Categories a human should always read before anything leaves, whatever else
# the evidence says. Dispute because it is a relationship problem, not a
# billing one; UpdateDetails because its template claims we have changed our
# records, which is only true once somebody actually has.
ALWAYS_REVIEW = {"Dispute", "UpdateDetails"}


def blockers(subject: str, body: str, to_address: str | None) -> list[dict[str, str]]:
    """Reasons a draft cannot be created. Empty means it can."""
    out: list[dict[str, str]] = []

    if not (to_address or "").strip():
        out.append({
            "code": "no_recipient",
            "message": "This email has no usable reply address.",
        })

    remaining = sorted(set(unfilled(body)) | set(unfilled(subject)))
    if remaining:
        listed = ", ".join("{{" + name + "}}" for name in remaining)
        out.append({
            "code": "unfilled_slots",
            "message": (
                f"The draft still contains {listed}. Fill it from the invoice "
                f"record, or delete the placeholder text to send without it."
            ),
        })

    return out


def flags(classification: Classification | None, lookup: dict[str, Any] | None,
          is_reply_to_us: bool, has_open_dispute: bool,
          category_key: str | None = None) -> list[dict[str, str]]:
    """Everything about this email that deserves a second look."""
    out: list[dict[str, str]] = []
    found = lookup or {}

    if classification is None or classification.source == "fallback":
        out.append({
            "level": "warn",
            "code": "not_classified",
            "message": (
                "This email was not classified — the classifier was "
                "unavailable. Choose a category yourself."
            ),
        })
    else:
        if classification.confidence < CATEGORY_CONFIDENCE_FLOOR:
            out.append({
                "level": "warn",
                "code": "low_confidence",
                "message": (
                    f"Low confidence in the category "
                    f"({classification.confidence:.0%}). Check it before sending."
                ),
            })
        if classification.multi_intent:
            out.append({
                "level": "warn",
                "code": "multi_intent",
                "message": (
                    f"This email raises a second, different request "
                    f"({classification.secondary}). One template cannot answer "
                    f"both."
                ),
            })
        if not classification.language_supported:
            out.append({
                "level": "warn",
                "code": "unsupported_language",
                "message": (
                    f"Written in '{classification.language}'. The templates are "
                    f"English only — answer this one by hand."
                ),
            })

    outcome = found.get("outcome")
    if outcome == Outcome.AMBIGUOUS:
        out.append({
            "level": "warn",
            "code": "ambiguous_invoice",
            "message": found.get("error")
            or "More than one invoice matches that number, so none was chosen.",
        })
    elif outcome == Outcome.TIMED_OUT:
        out.append({
            "level": "warn",
            "code": "lookup_timed_out",
            "message": "Xero did not answer in time. Nothing was assumed — try "
                       "syncing again, or check the invoice yourself.",
        })
    elif outcome == Outcome.NOT_FOUND:
        out.append({
            "level": "warn",
            "code": "invoice_not_found",
            "message": found.get("error")
            or "No matching invoice found in Xero.",
        })
    elif outcome == Outcome.ERROR:
        out.append({
            "level": "warn",
            "code": "lookup_failed",
            "message": found.get("error") or "The Xero lookup failed.",
        })
    elif outcome == Outcome.SKIPPED:
        out.append({
            "level": "info",
            "code": "no_invoice_number",
            "message": "No invoice number was mentioned, so nothing was looked "
                       "up. Any open invoices for this sender are listed below.",
        })

    for mismatch in (found.get("mismatches") or []):
        field = mismatch.get("field")
        if field == "amount":
            out.append({
                "level": "warn",
                "code": "amount_mismatch",
                "message": (
                    f"The customer says {mismatch.get('claimed')}; Xero says "
                    f"{mismatch.get('actual')} "
                    f"({mismatch.get('outstanding')} outstanding). Xero's "
                    f"figure is the one in the draft."
                ),
            })
        elif field == "invoiceNumber":
            out.append({
                "level": "warn",
                "code": "invoice_number_mismatch",
                "message": (
                    f"The customer wrote {mismatch.get('claimed')}; the matched "
                    f"invoice is {mismatch.get('actual')}."
                ),
            })

    if is_reply_to_us:
        out.append({
            "level": "info",
            "code": "reply_to_us",
            "message": "This is a reply to something we already sent — read the "
                       "thread before answering again.",
        })

    if has_open_dispute:
        out.append({
            "level": "warn",
            "code": "open_dispute",
            "message": "This customer has an open dispute. Check where it stands "
                       "before replying about anything else.",
        })

    key = category_key or (classification.category if classification else None)
    if key in ALWAYS_REVIEW:
        out.append({
            "level": "info",
            "code": "always_review",
            "message": (
                "Disputes are answered by a person, and the "
                "contact-details reply says we have already updated our "
                "records — make sure that is true."
                if key == "UpdateDetails" else
                "A dispute is a relationship problem. The template only "
                "acknowledges it; someone still has to act on it."
            ),
        })

    return out
