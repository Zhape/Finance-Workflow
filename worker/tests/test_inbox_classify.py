"""What happens to whatever the model returns.

Every test here is about not trusting the answer. The response schema makes
prose unrepresentable, but a schema is a request, not a guarantee — models
return off-enum values, omit confidences, and answer confidently in languages
nobody here can check. These are the rules that catch each of those.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.inbox import classify  # noqa: E402
from fw.inbox.models import (  # noqa: E402
    OUT_OF_SCOPE,
    SYSTEM_CATEGORIES,
    CategoryDef,
)

CATEGORIES = list(SYSTEM_CATEGORIES)


def answer(**overrides):
    base = {
        "category": "InvoiceQuery",
        "confidence": 0.93,
        "secondary_category": "None",
        "secondary_confidence": 0.0,
        "language": "en",
        "invoice_number": "INV-1042",
        "invoice_number_confidence": 0.95,
        "amount": "3500.00",
        "amount_confidence": 0.9,
        "customer_name": "Bob Smith",
        "customer_name_confidence": 0.88,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------

def test_the_schema_enum_is_built_from_the_orgs_own_categories():
    """This is what lets a customer add a bucket without a deploy."""
    extra = CategoryDef(key="Refund", label="Wants a refund",
                        description="The customer is asking for money back.")
    schema = classify.response_schema(CATEGORIES + [extra])

    assert "Refund" in schema["properties"]["category"]["enum"]
    assert schema["properties"]["category"]["type"] == "string"


def test_a_disabled_category_is_not_offered_to_the_model():
    subset = [c for c in CATEGORIES if c.key != "Dispute"]
    assert "Dispute" not in classify.response_schema(subset)["properties"][
        "category"]["enum"]


def test_the_prompt_carries_each_categorys_description():
    """The description is how an org teaches the classifier its new bucket."""
    text = classify.prompt("Subject", "Body", CATEGORIES)
    for category in CATEGORIES:
        assert category.key in text
        assert category.description[:40] in text


# ---------------------------------------------------------------------------
# Defences
# ---------------------------------------------------------------------------

def test_a_hallucinated_category_becomes_out_of_scope():
    """UAT: a label outside the defined set is never acted on as if it were
    one of them."""
    result = classify.coerce(answer(category="RefundRequest"), CATEGORIES, "m")
    assert result.category == OUT_OF_SCOPE


def test_prose_where_a_category_was_asked_for_becomes_out_of_scope():
    result = classify.coerce(
        answer(category="I think the customer wants a refund"), CATEGORIES, "m"
    )
    assert result.category == OUT_OF_SCOPE


def test_a_field_below_the_confidence_floor_is_discarded():
    """A wrong invoice number quoted back is worse than none at all."""
    result = classify.coerce(
        answer(invoice_number="INV-9", invoice_number_confidence=0.4),
        CATEGORIES, "m",
    )
    assert result.extracted.invoice_number is None
    assert result.extracted.amount == 3500.0     # the confident one survives


def test_a_field_with_no_confidence_score_is_not_trusted():
    raw = answer()
    del raw["invoice_number_confidence"]
    result = classify.coerce(raw, CATEGORIES, "m")
    assert result.extracted.invoice_number is None


def test_a_strong_secondary_category_marks_the_email_multi_intent():
    """'I've paid 1042, and I'm disputing 1043' — the worst failure mode is
    answering only the first half."""
    result = classify.coerce(
        answer(category="Confirmation", secondary_category="Dispute",
               secondary_confidence=0.55),
        CATEGORIES, "m",
    )
    assert result.multi_intent is True
    assert result.secondary == "Dispute"


def test_a_weak_secondary_category_does_not():
    result = classify.coerce(
        answer(secondary_category="Dispute", secondary_confidence=0.15),
        CATEGORIES, "m",
    )
    assert result.multi_intent is False


def test_a_secondary_matching_the_primary_is_ignored():
    result = classify.coerce(
        answer(category="Dispute", secondary_category="Dispute",
               secondary_confidence=0.9),
        CATEGORIES, "m",
    )
    assert result.secondary is None
    assert result.multi_intent is False


def test_an_unsupported_language_is_detected_rather_than_classified_badly():
    result = classify.coerce(answer(language="fr"), CATEGORIES, "m")
    assert result.language_supported is False


def test_english_is_supported():
    assert classify.coerce(answer(), CATEGORIES, "m").language_supported


def test_the_model_version_is_recorded_on_every_classification():
    """So an accuracy regression can be attributed to a model change."""
    result = classify.coerce(answer(), CATEGORIES, "gemini-2.5-flash", 812)
    assert result.model_version == "gemini-2.5-flash"
    assert result.latency_ms == 812


def test_confidence_is_clamped_into_range():
    assert classify.coerce(answer(confidence=4.2), CATEGORIES, "m").confidence == 1.0
    assert classify.coerce(answer(confidence=-1), CATEGORIES, "m").confidence == 0.0
    assert classify.coerce(answer(confidence="tbd"), CATEGORIES, "m").confidence == 0.0


# ---------------------------------------------------------------------------
# Amounts, as customers actually write them
# ---------------------------------------------------------------------------

def test_amounts_are_parsed_out_of_how_people_write_them():
    cases = {
        "£3,500.00": 3500.0,
        "3500": 3500.0,
        "AUD 1,234.56": 1234.56,
        "1.234,56": 1234.56,     # European convention
        "$99": 99.0,
    }
    for written, expected in cases.items():
        result = classify.coerce(
            answer(amount=written, amount_confidence=0.9), CATEGORIES, "m"
        )
        assert result.extracted.amount == expected, written


def test_an_unparseable_amount_is_dropped_not_guessed():
    result = classify.coerce(
        answer(amount="about three thousand", amount_confidence=0.9),
        CATEGORIES, "m",
    )
    assert result.extracted.amount is None


# ---------------------------------------------------------------------------
# Degraded mode
# ---------------------------------------------------------------------------

def test_the_fallback_routes_to_a_person_with_a_holding_template():
    """An outage costs triage speed, never correctness."""
    result = classify.fallback("CLS-001")
    assert result.category == OUT_OF_SCOPE
    assert result.confidence == 0.0
    assert result.source == "fallback"
    assert classify.low_confidence(result)
