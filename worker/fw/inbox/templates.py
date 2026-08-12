"""The approved wording, and the slots it is allowed to reference.

This module is the entire vocabulary of things the product can say to a
customer. The classifier chooses which template; the renderer fills the slots
from Xero. Neither can add a word that is not written here or in an org's own
override.

Placeholders are `{{double_braced}}` — the form the business wrote them in.
`render.py` fills them with a regex rather than `str.format`, so a stray brace
in a template is a visible placeholder rather than a KeyError at the end of a
sync.
"""

from __future__ import annotations

from .models import OUT_OF_SCOPE

# Every slot a template may reference, with where the value comes from. Xero is
# the source for all of them but two: the salutation (which falls back) and the
# sender's name (which is the person operating the tool).
PLACEHOLDERS: list[tuple[str, str]] = [
    ("{{customer_name}}", "Customer name from the Xero contact"),
    ("{{invoice_number}}", "Invoice number as it appears in Xero"),
    ("{{amount}}", "Invoice total, formatted in the Xero currency"),
    ("{{currency}}", "Currency code from Xero, e.g. GBP"),
    ("{{due_date}}", "Due date from Xero"),
    ("{{outstanding_balance}}", "Still owed after credit notes and part payments"),
    ("{{status}}", "Invoice status in Xero, e.g. AUTHORISED"),
    ("{{description}}", "What the invoice is for, from its line items"),
    ("{{summary}}", "Line items condensed, for invoices with a long list"),
    ("{{invoice_pdf}}", "Link to the invoice in Xero's online view"),
    ("{{sender}}", "Name the reply is signed with"),
]

PLACEHOLDER_NAMES = [token.strip("{}") for token, _ in PLACEHOLDERS]

# Slots whose value is not on the invoice record, so a missing invoice must not
# hold up a reply that never mentions one.
NON_INVOICE_SLOTS = {"customer_name", "sender"}

# `{{summary}}` was specified as an AI-written condensation of a long line-item
# list. It is built in `render.describe()` instead — first few descriptions plus
# a count of the rest — because a deterministic condensation solves the actual
# problem (a forty-line invoice becoming a wall of text) while keeping the
# stronger guarantee: no character of an outbound email is ever model-written.


DEFAULT_SUBJECTS: dict[str, str] = {
    "PayConfirmation": "Re: {{invoice_number}}",
    "Confirmation": "Re: {{invoice_number}}",
    "PaymentPromise": "Re: {{invoice_number}}",
    "InvoiceQuery": "Re: {{invoice_number}}",
    "Dispute": "Re: {{invoice_number}}",
    "UpdateDetails": "Re: your contact details",
    OUT_OF_SCOPE: "Re: your message",
}

DEFAULT_BODIES: dict[str, str] = {
    "PayConfirmation": (
        "Hi {{customer_name}},\n\n"
        "Thanks for sending this through — we've received your remittance and "
        "will match it against {{invoice_number}}. We'll come back to you only "
        "if anything doesn't line up.\n\n"
        "Nothing further is needed from your side.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    "Confirmation": (
        "Hi {{customer_name}},\n\n"
        "Thanks for letting us know! We'll keep an eye out for it and let you "
        "know of any issues. Could you share any remittance details, such as "
        "the payment date, amount, and reference number - so we can track it "
        "down as quickly as possible?\n\n"
        "Nothing further is needed from your side.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    "PaymentPromise": (
        "Hi {{customer_name}},\n\n"
        "Thanks for letting us know the expected payment date! We'll keep an "
        "eye out for it. If anything changes in the meantime, just give us a "
        "heads-up and we'll work with you from there.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    "InvoiceQuery": (
        "Hi {{customer_name}},\n\n"
        "Happy to help. {{invoice_number}} is for {{description}}, and the "
        "amount outstanding is {{currency}} {{outstanding_balance}}.\n\n"
        "Just let us know if you'd like a copy of the invoice resent.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    "Dispute": (
        "Hi {{customer_name}},\n\n"
        "Thanks for getting in touch about {{invoice_number}}. We're sorry to "
        "hear there's an issue. We've logged this and a member of the team "
        "will review the details and come back to you shortly.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    "UpdateDetails": (
        "Hi {{customer_name}},\n\n"
        "Thanks - we've updated our records with your new accounts contact "
        "details. All future invoices and statements will go to the new "
        "address from now on.\n\n"
        "Kind regards,\n{{sender}}"
    ),
    OUT_OF_SCOPE: (
        "Hi {{customer_name}},\n\n"
        "Thanks for your message. A member of the accounts team will follow up "
        "with you shortly.\n\n"
        "Kind regards,\n{{sender}}"
    ),
}

# A template an org adds for a category of its own starts from this. Neutral on
# purpose: it promises a person will follow up, and nothing else.
BLANK_TEMPLATE = {
    "subject": "Re: your message",
    "body": (
        "Hi {{customer_name}},\n\n"
        "Thanks for your message. A member of the accounts team will follow up "
        "with you shortly.\n\n"
        "Kind regards,\n{{sender}}"
    ),
}


def default_templates() -> dict[str, dict[str, str]]:
    """The wording this product ships with, in the shape the editor expects.

    Absence of an org override means the org keeps getting improvements to
    these words — the same rule the chase-up follows.
    """
    return {
        key: {"subject": DEFAULT_SUBJECTS[key], "body": body}
        for key, body in DEFAULT_BODIES.items()
    }


def default_for(category_key: str) -> dict[str, str]:
    """The shipped template for a category, or a neutral holding reply."""
    if category_key in DEFAULT_BODIES:
        return {
            "subject": DEFAULT_SUBJECTS[category_key],
            "body": DEFAULT_BODIES[category_key],
        }
    return dict(BLANK_TEMPLATE)
