"""Vocabulary for the Invoice Inbox: states, categories, error codes.

Everything the rest of the package agrees on lives here, so the classifier,
the renderer and the API cannot drift into using different spellings of the
same idea.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KEY = "invoice-inbox"

# The one category that must always exist and can never be disabled. Anything
# the model returns that we do not recognise lands here, so an unknown label is
# a routing decision rather than a crash.
OUT_OF_SCOPE = "OutOfScope"

# A secondary category scoring at or above this makes the email multi-intent
# and forces manual attention. "I've paid 1042, and I'm disputing 1043" is the
# case: single-label classification would answer only the first half.
MULTI_INTENT_THRESHOLD = 0.4

# Extracted fields below this are discarded rather than shown. A wrong invoice
# number quoted back to a customer is worse than no invoice number.
FIELD_CONFIDENCE_FLOOR = 0.6

# Below this the category itself is not trusted enough to preselect a template.
CATEGORY_CONFIDENCE_FLOOR = 0.6

SUPPORTED_LANGUAGES = {"en", "eng", "english", ""}


class State:
    """Where an email is. One value per row in inbox_emails."""

    RECEIVED = "received"          # ingested, not yet triaged
    SUPPRESSED = "suppressed"      # auto-reply, bounce, or our own mail
    NEEDS_REVIEW = "needs_review"  # triaged, waiting on a person
    DRAFTED = "drafted"            # a Gmail draft exists in the thread
    DRAFT_FAILED = "draft_failed"  # Gmail refused; queued and surfaced
    DISMISSED = "dismissed"        # closed without replying


class Outcome:
    """What the Xero lookup found. Recorded on every single email."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"        # treated as not found, never "first result wins"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"            # no invoice number was extracted
    ERROR = "error"


class Code:
    """Error codes, so a failure can be talked about without prose.

    Stage prefixes: ING ingest, CLS classification, XRO lookup, TPL rendering,
    DRF draft creation.
    """

    ING_FETCH_FAILED = "ING-001"
    ING_PARSE_FAILED = "ING-002"
    CLS_TIMEOUT = "CLS-001"
    CLS_BAD_CATEGORY = "CLS-002"      # model returned something off-schema
    CLS_UNAVAILABLE = "CLS-003"       # no key configured, or circuit open
    CLS_MALFORMED = "CLS-004"
    XRO_TIMEOUT = "XRO-001"
    XRO_FAILED = "XRO-002"
    TPL_MISSING = "TPL-001"           # no template for this category
    DRF_FAILED = "DRF-001"
    DRF_NO_MAILBOX = "DRF-002"


@dataclass(frozen=True)
class CategoryDef:
    """One classification bucket.

    Shipped as a row in inbox_categories rather than as code, because the
    product promise is that a customer can add a bucket. `description` is
    written for the model, not for the screen: it is the text the classifier
    reads to decide whether an email belongs here.
    """

    key: str
    label: str
    description: str
    is_system: bool = False
    enabled: bool = True
    sort_order: int = 100

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "isSystem": self.is_system,
            "enabled": self.enabled,
            "sortOrder": self.sort_order,
        }


# The seven shipped buckets. Seeded per org on first connect; an org may
# disable any of them except OutOfScope, and may add its own.
SYSTEM_CATEGORIES: list[CategoryDef] = [
    CategoryDef(
        key="PayConfirmation",
        label="Remittance received",
        description=(
            "The customer has sent remittance advice or proof of payment — a "
            "payment reference, a bank confirmation, or an attached remittance "
            "document. Stronger evidence than simply saying they have paid."
        ),
        is_system=True, sort_order=10,
    ),
    CategoryDef(
        key="Confirmation",
        label="Says they have paid",
        description=(
            "The customer states that they have already made the payment, but "
            "without attaching remittance advice or a payment reference."
        ),
        is_system=True, sort_order=20,
    ),
    CategoryDef(
        key="PaymentPromise",
        label="Will pay on a date",
        description=(
            "The customer is telling us when they intend to pay — a date, a "
            "payment run, or a period such as 'end of the month'. They have "
            "not paid yet."
        ),
        is_system=True, sort_order=30,
    ),
    CategoryDef(
        key="InvoiceQuery",
        label="What is this for?",
        description=(
            "The customer wants to understand what the invoice covers: what "
            "the charge relates to, which period or service, or a request for "
            "a copy of the invoice."
        ),
        is_system=True, sort_order=40,
    ),
    CategoryDef(
        key="Dispute",
        label="Disputing or cancelling",
        description=(
            "The customer disagrees with the invoice, believes it is wrong, "
            "refuses to pay it, or wants to cancel a service or subscription."
        ),
        is_system=True, sort_order=50,
    ),
    CategoryDef(
        key="UpdateDetails",
        label="New contact details",
        description=(
            "The customer is telling us to change where invoices or statements "
            "go: a new accounts email address, a new contact person, or a new "
            "billing address."
        ),
        is_system=True, sort_order=60,
    ),
    CategoryDef(
        key=OUT_OF_SCOPE,
        label="Out of scope",
        description=(
            "Anything that is not about an invoice or a payment — sales "
            "enquiries, marketing, newsletters, internal mail, or anything "
            "that does not fit the other categories. Use this whenever no "
            "other category clearly applies."
        ),
        is_system=True, sort_order=999,
    ),
]

SYSTEM_KEYS = {c.key for c in SYSTEM_CATEGORIES}


@dataclass
class Extracted:
    """What the model believes the customer said.

    Displayed as secondary information throughout. Xero is what gets injected
    into a template; this is only ever a hint about which Xero record to fetch.
    """

    invoice_number: str | None = None
    amount: float | None = None
    currency: str | None = None
    customer_name: str | None = None
    mentioned_date: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "invoiceNumber": self.invoice_number,
            "amount": self.amount,
            "currency": self.currency,
            "customerName": self.customer_name,
            "mentionedDate": self.mentioned_date,
        }


@dataclass
class Classification:
    category: str
    confidence: float
    secondary: str | None = None
    secondary_confidence: float | None = None
    multi_intent: bool = False
    language: str = "en"
    extracted: Extracted = field(default_factory=Extracted)
    model_version: str = ""
    latency_ms: int = 0
    source: str = "ai"

    @property
    def language_supported(self) -> bool:
        return (self.language or "").strip().lower() in SUPPORTED_LANGUAGES
