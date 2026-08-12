"""The classification port, and the rules applied to whatever comes back.

`ClassificationClient` is the interface; `gemini.py` is the only implementation.
Everything defensive lives here rather than in the client, so the rules hold
whichever model is behind it — including a model that has just been swapped for
a newer one and started answering slightly differently.

The defences, in the order they fire:

  1. A category outside the org's enabled set becomes OutOfScope. The response
     schema already constrains it to an enum; this is what happens when the
     schema is not honoured, which is a thing models do.
  2. A field below the confidence floor becomes None. A wrong invoice number
     quoted back to a customer is worse than no invoice number.
  3. A secondary category above the multi-intent threshold marks the email
     multi-intent. "I've paid 1042, and I'm disputing 1043" must never be
     answered as if it were only the first half.
  4. An unsupported language routes to manual with a reason, rather than being
     classified badly in a language nobody here can check.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from .models import (
    CATEGORY_CONFIDENCE_FLOOR,
    Classification,
    CategoryDef,
    Extracted,
    FIELD_CONFIDENCE_FLOOR,
    MULTI_INTENT_THRESHOLD,
    OUT_OF_SCOPE,
)


class ClassificationError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class ClassificationClient(Protocol):
    """One call: read an email, return a label and what it mentions."""

    @property
    def model_version(self) -> str: ...

    def classify(self, subject: str, body: str,
                 categories: list[CategoryDef]) -> dict[str, Any]:
        """Return the raw structured response. Coercion happens in `coerce`."""
        ...


def response_schema(categories: list[CategoryDef]) -> dict[str, Any]:
    """The JSON schema the model must answer in.

    Built from the org's enabled categories at call time, which is what lets a
    customer add a bucket without a deploy. The enum is the point: prose is not
    a representable answer.
    """
    keys = [c.key for c in categories] or [OUT_OF_SCOPE]
    number = {"type": "number"}
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": keys},
            "confidence": number,
            "secondary_category": {"type": "string", "enum": keys + ["None"]},
            "secondary_confidence": number,
            "language": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_number_confidence": number,
            "amount": {"type": "string"},
            "amount_confidence": number,
            "currency": {"type": "string"},
            "customer_name": {"type": "string"},
            "customer_name_confidence": number,
            "mentioned_date": {"type": "string"},
            "mentioned_date_confidence": number,
        },
        "required": ["category", "confidence", "language"],
    }


def prompt(subject: str, body: str, categories: list[CategoryDef]) -> str:
    """The instruction. Deliberately has no room for an opinion.

    Category descriptions come from the database, so an org that adds a bucket
    also teaches the classifier what belongs in it.
    """
    catalogue = "\n".join(
        f"- {c.key}: {c.description}" for c in categories
    )
    return (
        "You are classifying an email sent by a customer to a company's "
        "accounts-receivable inbox. Assign exactly one category and extract "
        "only what the email itself states.\n\n"
        "Categories:\n"
        f"{catalogue}\n\n"
        "Rules:\n"
        "- Use the category that best matches the customer's main request. If "
        f"nothing fits, use {OUT_OF_SCOPE}.\n"
        "- If the email raises a clear second, different request, put it in "
        "secondary_category. Otherwise set secondary_category to \"None\".\n"
        "- Extract values only if the email states them. Never infer, "
        "calculate or complete a partial invoice number.\n"
        "- Give each extracted field a confidence between 0 and 1. Use a low "
        "confidence when you are unsure; do not guess to fill a field.\n"
        "- language is the ISO 639-1 code of the language the email is "
        "written in.\n\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body}"
    )


def _number(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    # Models return amounts as written: "£3,500.00", "3 500,00", "AUD 1,234".
    text = re.sub(r"[^\d.,-]", "", str(raw)).strip()
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") \
            else text.replace(".", "").replace(",", ".")
    elif "," in text:
        # A single comma with exactly two digits after it is a decimal comma.
        text = text.replace(",", "." if re.search(r",\d{2}$", text) else "")
    try:
        return float(text)
    except ValueError:
        return None


def _confident(raw: dict[str, Any], field: str) -> Any:
    """A field's value, or None when the model was not confident enough."""
    value = raw.get(field)
    if value is None or str(value).strip() == "":
        return None
    score = raw.get(f"{field}_confidence")
    try:
        score = float(score)
    except (TypeError, ValueError):
        # A missing score is not permission to trust the value.
        return None
    if score < FIELD_CONFIDENCE_FLOOR:
        return None
    return str(value).strip()


def coerce(raw: dict[str, Any], categories: list[CategoryDef],
           model_version: str, latency_ms: int = 0) -> Classification:
    """Turn a raw model response into something the rest of the app can trust."""
    allowed = {c.key for c in categories}

    category = str(raw.get("category") or "").strip()
    if category not in allowed:
        # Off-schema answer. Not an error to raise — a routing decision, so the
        # email still reaches a person with a template attached.
        category = OUT_OF_SCOPE

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    secondary = str(raw.get("secondary_category") or "").strip()
    try:
        secondary_confidence = float(raw.get("secondary_confidence"))
    except (TypeError, ValueError):
        secondary_confidence = 0.0
    if secondary in ("", "None", category) or secondary not in allowed:
        secondary, secondary_confidence = None, None

    multi_intent = bool(
        secondary and (secondary_confidence or 0) >= MULTI_INTENT_THRESHOLD
    )

    amount = _confident(raw, "amount")
    extracted = Extracted(
        invoice_number=_confident(raw, "invoice_number"),
        amount=_number(amount) if amount is not None else None,
        currency=(str(raw.get("currency") or "").strip() or None),
        customer_name=_confident(raw, "customer_name"),
        mentioned_date=_confident(raw, "mentioned_date"),
    )

    return Classification(
        category=category,
        confidence=confidence,
        secondary=secondary,
        secondary_confidence=secondary_confidence,
        multi_intent=multi_intent,
        language=str(raw.get("language") or "en").strip().lower(),
        extracted=extracted,
        model_version=model_version,
        latency_ms=latency_ms,
        source="ai",
    )


def fallback(reason: str) -> Classification:
    """What every email gets when classification could not run at all.

    Degraded mode is not an outage: mail still arrives, still reaches a person,
    and still has a holding template attached. Only the triage stops.
    """
    return Classification(
        category=OUT_OF_SCOPE,
        confidence=0.0,
        language="en",
        model_version=reason,
        source="fallback",
    )


def low_confidence(classification: Classification) -> bool:
    return classification.confidence < CATEGORY_CONFIDENCE_FLOOR
