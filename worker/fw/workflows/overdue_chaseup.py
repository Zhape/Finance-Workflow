"""Overdue chase-up — who owes money, how late, and what to say to them.

The hosted version of the desktop overdue-chaseup app, with two deliberate
differences.

**It drafts, it does not send.** With Gmail connected, approving a run creates
a draft per customer in the connected mailbox, ready to read and send. The
Gmail scope requested (`gmail.compose`) cannot send at all, so this is a
ceiling enforced by Google rather than a promise in a comment. A chase is a
relationship, and the person whose name is on the email should see it before
the customer does.

**It does not scrape the platform.** The desktop app drives a browser to look
up account admins, and that flow needs an interactive login it cannot perform
headless. Contact details come from Xero instead, and rows without an email
are flagged rather than silently dropped.

What survives from the desktop app is the part with judgement in it: ageing
buckets, one row per customer rather than per invoice, and a tone that escalates
with lateness.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ..contract import (
    Column,
    ParamSpec,
    RunContext,
    RunResult,
    RunStatus,
    WorkflowSpec,
)
from ..xero import XeroClient

SPEC = WorkflowSpec(
    key="overdue-chaseup",
    name="Overdue Chase-Up",
    description=(
        "Find customers with overdue invoices, grouped and aged, with a "
        "drafted chase message for each."
    ),
    integrations=["xero"],
    requires_approval=True,
    params=[
        ParamSpec(
            name="connection",
            type="choice",
            label="Xero organisation",
            options=["default", "us"],
            default="default",
            help="Which connected Xero organisation to read receivables from.",
        ),
        ParamSpec(
            name="min_days_overdue",
            type="choice",
            label="Only chase invoices at least this overdue",
            options=["1", "7", "14", "30", "60"],
            default="7",
            help="Days past the due date. 7 avoids chasing invoices that "
                 "crossed in the post.",
        ),
        ParamSpec(
            name="min_amount",
            type="string",
            label="Ignore balances under",
            required=False,
            default="0",
            help="Total owed per customer. Leave at 0 to include everything.",
        ),
    ],
)

# Ageing buckets, and the tone that goes with each. Kept here rather than in a
# template file because the wording IS the business rule: what you say at 90
# days is not what you say at 10.
BUCKETS: list[tuple[int, str, str]] = [
    (90, "90+ days", "final"),
    (60, "60-89 days", "firm"),
    (30, "30-59 days", "reminder"),
    (0, "1-29 days", "gentle"),
]

# Shipped defaults. An org can override any of these in Settings; absence of
# an override means it keeps getting improvements to this wording.
#
# Placeholders are deliberately few and obvious. Every one is something the
# workflow knows for certain, because a template that can reference a field
# that is sometimes missing produces "Hi {first}," in a customer's inbox.
PLACEHOLDERS = [
    ("{first}", "Customer's first name, or the company name"),
    ("{customer}", "Full customer name as it appears in Xero"),
    ("{count}", "How many invoices are overdue"),
    ("{amount}", "Total owed, formatted (e.g. 6,000.00)"),
    ("{currency}", "Currency code (e.g. GBP)"),
    ("{days}", "Days past due on the oldest invoice"),
    ("{invoices}", "Invoice numbers, comma separated"),
    ("{sender}", "Name the message is signed with"),
]

DEFAULT_SUBJECTS = {
    "gentle": "Overdue invoice{plural} - {currency} {amount}",
    "reminder": "Reminder: {currency} {amount} overdue",
    "firm": "Overdue account - {currency} {amount} - response needed",
    "final": "Final notice: {currency} {amount} overdue",
}

TEMPLATES = {
    "gentle": (
        "Hi {first},\n\n"
        "Hope you're well. Our records show {count} invoice(s) totalling "
        "{currency} {amount} now past due — the oldest by {days} days.\n\n"
        "If it's already been paid, apologies and please ignore this. "
        "Otherwise, could you let me know when we can expect payment?\n\n"
        "Thanks,\n{sender}"
    ),
    "reminder": (
        "Hi {first},\n\n"
        "Following up on {count} overdue invoice(s) totalling {currency} "
        "{amount}. The oldest is now {days} days past due.\n\n"
        "Could you confirm when payment will be made, or let me know if "
        "there's a query holding it up?\n\n"
        "Thanks,\n{sender}"
    ),
    "firm": (
        "Hi {first},\n\n"
        "{count} invoice(s) totalling {currency} {amount} remain unpaid, the "
        "oldest {days} days past due. This is now well outside our agreed "
        "terms.\n\n"
        "Please arrange payment this week, or reply today with a date we can "
        "hold you to.\n\n"
        "Regards,\n{sender}"
    ),
    "final": (
        "Hi {first},\n\n"
        "Despite previous reminders, {count} invoice(s) totalling {currency} "
        "{amount} remain unpaid — the oldest {days} days past due.\n\n"
        "Please treat this as urgent. If payment is not received or a plan "
        "agreed, we will need to escalate and may place the account on hold.\n\n"
        "Regards,\n{sender}"
    ),
}


def parse_xero_date(value: Any) -> date | None:
    """Xero returns either "/Date(ms+offset)/" or an ISO string."""
    import re

    if not value:
        return None
    s = str(value).strip()
    m = re.search(r"/Date\((-?\d+)", s)
    if m:
        try:
            return datetime.fromtimestamp(
                int(m.group(1)) / 1000, tz=timezone.utc
            ).date()
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(s.split("T")[0]).date()
    except ValueError:
        return None


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bucket_for(days: int) -> tuple[str, str]:
    for threshold, label, tone in BUCKETS:
        if days >= threshold:
            return label, tone
    return BUCKETS[-1][1], BUCKETS[-1][2]


def first_name(contact_name: str) -> str:
    """Best-effort first name for a greeting, falling back to the full name.

    A company name is left whole: "Hi Acme Ltd" reads oddly but "Hi Acme" reads
    like a mistake, and a wrong-looking greeting is what gets a chase ignored.
    """
    name = (contact_name or "").strip()
    if not name:
        return "there"
    parts = name.split()
    if len(parts) > 1 and parts[0].isalpha() and parts[0].istitle() and len(parts) <= 3:
        return parts[0]
    return name


def default_templates() -> dict[str, dict[str, str]]:
    """The wording this workflow ships with, in the shape the editor expects."""
    return {
        tone: {"subject": DEFAULT_SUBJECTS[tone], "body": TEMPLATES[tone]}
        for tone in TEMPLATES
    }


def render(template: str, values: dict[str, Any]) -> str:
    """Fill a template, leaving unknown placeholders visible rather than
    exploding. Someone editing wording should see `{typo}` in the preview, not
    a 500 at the end of a run."""

    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return template.format_map(_Safe(values))


def run(params: dict[str, Any], ctx: RunContext) -> RunResult:
    log: list[str] = []
    warnings: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        ctx.log(msg)

    connection = params.get("connection", "default")
    min_days = int(params.get("min_days_overdue") or 0)
    min_amount = to_float(params.get("min_amount") or 0)
    today = date.today()

    note(f"Connecting to Xero ({connection})…")
    access_token, tenant_id = ctx.creds.xero(connection)
    client = XeroClient(access_token, tenant_id)

    note("Pulling unpaid customer invoices…")
    invoices = client.get_receivables()
    note(f"{len(invoices)} invoice(s) returned by Xero")

    # Only invoices with something still owed. Xero can return AUTHORISED
    # invoices already settled by a credit note or payment.
    invoices = [i for i in invoices if to_float(i.get("AmountDue")) > 0]
    note(f"{len(invoices)} with a balance outstanding")

    # Group by customer: one chase per customer, not one per invoice. Chasing
    # the same person four times in four emails is how a finance team loses
    # goodwill it needs later.
    by_contact: dict[str, dict[str, Any]] = {}
    for inv in invoices:
        due = parse_xero_date(inv.get("DueDateString") or inv.get("DueDate"))
        if due is None:
            continue
        days = (today - due).days
        if days < min_days:
            continue

        contact = inv.get("Contact") or {}
        cid = contact.get("ContactID") or contact.get("Name") or "unknown"
        entry = by_contact.setdefault(cid, {
            "name": str(contact.get("Name") or "").strip(),
            "email": str(contact.get("EmailAddress") or "").strip() or None,
            "currency": str(inv.get("CurrencyCode") or ""),
            "amount": 0.0,
            "count": 0,
            "oldest_days": 0,
            "references": [],
        })
        entry["amount"] += to_float(inv.get("AmountDue"))
        entry["count"] += 1
        entry["oldest_days"] = max(entry["oldest_days"], days)
        ref = str(inv.get("InvoiceNumber") or inv.get("Reference") or "").strip()
        if ref:
            entry["references"].append(ref)

    note(f"{len(by_contact)} customer(s) past {min_days} days")

    sender = getattr(ctx, "sender_name", None) or "Accounts"
    templates = ctx.templates or default_templates()
    rows: list[dict[str, Any]] = []
    for cid, e in by_contact.items():
        if e["amount"] < min_amount:
            continue
        label, tone = bucket_for(e["oldest_days"])
        refs = ", ".join(dict.fromkeys(e["references"])) or None
        values = {
            "first": first_name(e["name"]),
            "customer": e["name"],
            "count": e["count"],
            "currency": e["currency"],
            "amount": f"{e['amount']:,.2f}",
            "days": e["oldest_days"],
            "invoices": refs or "",
            "sender": sender,
            "plural": "" if e["count"] == 1 else "s",
        }
        chosen = templates.get(tone) or default_templates()[tone]
        rows.append({
            "id": str(cid),
            "name": e["name"],
            "email": e["email"],
            "amount": round(e["amount"], 2),
            "currency": e["currency"],
            "count": e["count"],
            "oldest_days": e["oldest_days"],
            "bucket": label,
            "tone": tone,
            "references": refs,
            "contactable": bool(e["email"]),
            "subject": render(chosen.get("subject", ""), values),
            "message": render(chosen.get("body", ""), values),
        })

    # Oldest and largest first: that is the order a person would work the list.
    rows.sort(key=lambda r: (-r["oldest_days"], -r["amount"]))

    unreachable = [r["name"] for r in rows if not r["contactable"]]
    if unreachable:
        warnings.append(
            f"{len(unreachable)} customer(s) have no email address in Xero and "
            f"cannot be chased from here: " + ", ".join(sorted(unreachable))
        )

    if not rows:
        return RunResult(
            status=RunStatus.EMPTY,
            log=log,
            warnings=warnings,
            summary=f"Nothing overdue by {min_days} days or more.",
        )

    total = sum(r["amount"] for r in rows)
    return RunResult(
        status=RunStatus.NEEDS_APPROVAL,
        columns=[
            Column("name", "Customer"),
            Column("bucket", "Age"),
            Column("oldest_days", "Days late"),
            Column("count", "Invoices"),
            Column("amount", "Owed", "money"),
            Column("email", "Email"),
            Column("references", "Invoice numbers"),
            Column("contactable", "Contactable", "flag"),
        ],
        rows=rows,
        warnings=warnings,
        log=log,
        summary=(
            f"{len(rows)} customer(s) overdue, {total:,.2f} outstanding — "
            f"awaiting review."
        ),
    )


def finalise(
    params: dict[str, Any],
    rows: list[dict[str, Any]],
    ctx: RunContext,
    columns: list[str],
) -> RunResult:
    """Create a Gmail draft per approved customer, and the chase list as a CSV.

    Drafting is best-effort per row: one customer whose draft fails must not
    lose the other twenty. Failures are collected and reported rather than
    raised, and the CSV is produced either way — a run that half-worked should
    still hand back something usable.

    `columns` is ignored: unlike a pay run there is no bank template dictating
    layout. It stays in the signature because the platform calls every workflow
    the same way, and a workflow that quietly took different arguments would be
    a trap for the next one.
    """
    import csv
    import io

    drafted = 0
    failures: list[str] = []
    mailbox = None
    if ctx.mailer is not None:
        for r in rows:
            if not r.get("email"):
                continue
            try:
                ctx.mailer.create_draft(
                    r["email"],
                    r.get("subject") or "Overdue invoice",
                    r.get("message") or "",
                )
                drafted += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{r.get('name')}: {type(exc).__name__}")
        mailbox = getattr(ctx.mailer, "address", None)

    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    header = ["customer", "email", "currency", "amount_owed", "invoices",
              "days_overdue", "age_bucket", "invoice_numbers", "subject",
              "message"]
    writer.writerow(header)
    for r in rows:
        writer.writerow([
            r.get("name"), r.get("email") or "", r.get("currency"),
            f"{to_float(r.get('amount')):.2f}", r.get("count"),
            r.get("oldest_days"), r.get("bucket"),
            r.get("references") or "", r.get("subject") or "",
            r.get("message") or "",
        ])

    total = sum(to_float(r.get("amount")) for r in rows)
    stamp = datetime.now().strftime("%Y-%m-%d")

    if ctx.mailer is None:
        outcome = (f"{len(rows)} chase message(s) prepared, {total:,.2f} "
                   f"outstanding. Gmail is not connected, so nothing was "
                   f"drafted.")
    else:
        where = f" in {mailbox}" if mailbox else ""
        outcome = (f"{drafted} draft(s) created{where}, {total:,.2f} "
                   f"outstanding. Nothing has been sent — review and send from "
                   f"Gmail.")

    return RunResult(
        status=RunStatus.COMPLETE,
        rows=rows,
        warnings=([f"Could not draft for {len(failures)} customer(s): "
                   + ", ".join(failures[:5])] if failures else []),
        summary=outcome,
        artifact_name=f"chase-list-{stamp}.csv",
        artifact_bytes=buf.getvalue().encode("utf-8-sig"),
    )
