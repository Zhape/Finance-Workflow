"""Weekly pay run — pull approved Xero bills, review, produce a bank file.

The hosted version of the desktop WeeklyPayrun (xero-bill-exporter), minus
the Wise write path.  The output is a bank file the customer uploads
themselves, exactly as they do today with Send-to-bank-accounts(*).xlsx.
Nothing in this module can move money.

Business rules carried over verbatim from the desktop mapper, because they
are the part that took real time to get right:
  • Only ACCPAY (bills), never AR invoices.
  • Only the run's own currency.
  • References containing "expenses" collapse to the literal "expenses".
  • Vendor names matching the business-suffix regex are "Business", else "Person".
  • US pays in the target currency and consolidates to one line per vendor;
    UK and EU pay in the source currency, one line per bill.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from ..banking import BankDetailsSource
from ..contract import (
    Column,
    ParamSpec,
    RunContext,
    RunResult,
    RunStatus,
    WorkflowSpec,
)
from ..xero import XeroClient

# ---------------------------------------------------------------------------
# Run definitions
# ---------------------------------------------------------------------------
# Which Xero connection each region draws from, and its payment conventions.
# In the hosted product this is org configuration, not code -- it is a literal
# here only so the spike matches the desktop app exactly.

REGIONS: dict[str, dict[str, Any]] = {
    "UK": {"currency": "GBP", "connection": "default", "amount_currency": "source",
           "consolidate": False},
    "US": {"currency": "USD", "connection": "us", "amount_currency": "target",
           "consolidate": True},
    "EU": {"currency": "EUR", "connection": "default", "amount_currency": "source",
           "consolidate": False},
}

SPEC = WorkflowSpec(
    key="weekly-payrun",
    name="Weekly Pay Run",
    description=(
        "Pull approved bills from Xero for a region, review which to pay, "
        "and download a bank-ready payment file."
    ),
    integrations=["xero"],
    requires_approval=True,
    approve_label="Approve and build bank file",
    params=[
        ParamSpec(
            name="connection",
            type="choice",
            label="Xero organisation",
            required=False,
            # Filled in by the API from this org's connected organisations.
            # The region map below is the historical fallback: it named
            # connections 'default' and 'us', which described one customer's
            # Xero estate on one day and nothing since.
            options=[],
            default=None,
            help="Which connected Xero organisation to read bills from. "
                 "Leave blank to use the one this region has always used.",
        ),
        ParamSpec(
            name="region",
            type="choice",
            label="Region",
            options=list(REGIONS),
            default="UK",
            help="Determines currency, Xero organisation and file layout.",
        ),
        ParamSpec(
            name="due_before",
            type="date",
            label="Only bills due before",
            required=False,
            help="Leave blank to include every approved bill.",
        ),
        ParamSpec(
            name="include_unmatched",
            type="bool",
            label="Include vendors with no bank details",
            required=False,
            default=False,
            help="Off by default — unmatched vendors cannot be paid.",
        ),
    ],
)

_BUSINESS_RE = re.compile(
    r"\b(ltd|limited|llp|plc|llc|inc|corp|corporation|partnership|"
    r"associates|holdings|group|services|solutions|consulting|"
    r"management|enterprises|institute|foundation|charity|trust|"
    r"council|authority|hmrc|dwp|nhs)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Value helpers (from the desktop mapper)
# ---------------------------------------------------------------------------

def clean_reference(raw: str) -> str:
    return "expenses" if "expenses" in raw.lower() else raw.strip()


def receiver_type(name: str) -> str:
    return "Business" if (_BUSINESS_RE.search(name) or " & " in name) else "Person"


def parse_xero_date(value: Any) -> date | None:
    """Xero returns either "/Date(ms+offset)/" or an ISO string."""
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


# ---------------------------------------------------------------------------
# Phase 1 — pull and prepare
# ---------------------------------------------------------------------------

def run(params: dict[str, Any], ctx: RunContext) -> RunResult:
    region = params["region"]
    cfg = REGIONS[region]
    currency = cfg["currency"]
    log: list[str] = []
    warnings: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        ctx.log(msg)

    bank: BankDetailsSource = ctx.bank_details
    if bank is None:
        raise ValueError("No vendor bank details configured for this organisation.")

    # An explicit choice wins; otherwise the region's historical mapping.
    connection = params.get("connection") or cfg["connection"]
    note(f"Connecting to Xero ({connection})…")
    access_token, tenant_id = ctx.creds.xero(connection)
    client = XeroClient(access_token, tenant_id)

    note("Pulling approved bills…")
    bills = client.get_bills()
    note(f"{len(bills)} bill(s) returned by Xero")

    # Filter 1 — AP only.  Xero can return other types despite the Type filter.
    non_ap = [b for b in bills if b.get("Type") != "ACCPAY"]
    bills = [b for b in bills if b.get("Type") == "ACCPAY"]
    if non_ap:
        note(f"Excluded {len(non_ap)} non-AP invoice(s)")

    # Filter 2 — this run's currency.
    other_ccy = [b for b in bills if b.get("CurrencyCode") != currency]
    bills = [b for b in bills if b.get("CurrencyCode") == currency]
    for b in other_ccy:
        name = (b.get("Contact") or {}).get("Name", "Unknown")
        note(
            f"Excluded (non-{currency}): {name} "
            f"{b.get('CurrencyCode', '?')} {to_float(b.get('AmountDue')):,.2f}"
        )
    note(f"{len(bills)} {currency} bill(s) after currency filter")

    # Filter 3 — optional due-date cutoff.
    cutoff_raw = params.get("due_before")
    if cutoff_raw:
        cutoff = (
            cutoff_raw if isinstance(cutoff_raw, date)
            else datetime.fromisoformat(str(cutoff_raw)).date()
        )
        before = len(bills)
        bills = [
            b for b in bills
            if (d := parse_xero_date(b.get("DueDateString") or b.get("DueDate")))
            and d < cutoff
        ]
        note(f"Excluded {before - len(bills)} bill(s) due on or after {cutoff}")

    if not bills:
        return RunResult(
            status=RunStatus.EMPTY,
            log=log,
            summary=f"No {currency} bills to pay.",
        )

    rows = _prepare_rows(bills, bank, cfg)

    unmatched = sorted({r["name"] for r in rows if not r["matched"]})
    if unmatched:
        warnings.append(
            f"{len(unmatched)} vendor(s) have no bank details and cannot be paid: "
            + ", ".join(unmatched)
        )
    if not params.get("include_unmatched"):
        rows = [r for r in rows if r["matched"]]

    if cfg["consolidate"]:
        before = len(rows)
        rows = _consolidate_by_vendor(rows)
        note(f"Consolidated {before} bill(s) into {len(rows)} vendor payment(s)")

    total = sum(r["amount"] for r in rows)
    return RunResult(
        status=RunStatus.NEEDS_APPROVAL if rows else RunStatus.EMPTY,
        columns=_columns(bank),
        rows=rows,
        warnings=warnings,
        log=log,
        summary=f"{len(rows)} payment(s), {currency} {total:,.2f} — awaiting approval.",
    )


def _columns(bank: BankDetailsSource) -> list[Column]:
    cols = [
        Column("name", "Vendor"),
        Column("reference", "Reference"),
        Column("due_date", "Due", "date"),
        Column("amount", "Amount", "money"),
        Column("receiver_type", "Type"),
        Column("email", "Email"),
    ]
    # Banking fields vary by region (sort code vs ABA vs IBAN) so they are
    # discovered, never hard-coded.
    cols += [Column(k, k, "text") for k in bank.field_keys()]
    cols.append(Column("matched", "Bank details", "flag"))
    return cols


def _prepare_rows(
    bills: list[dict[str, Any]],
    bank: BankDetailsSource,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, bill in enumerate(bills):
        contact = bill.get("Contact") or {}
        name = str(contact.get("Name") or "").strip()
        details = bank.lookup(name)
        matched = any(bool(v) for v in details.values())
        due = parse_xero_date(bill.get("DueDateString") or bill.get("DueDate"))
        currency = str(bill.get("CurrencyCode") or cfg["currency"])

        rows.append({
            "id": bill.get("InvoiceID") or f"row-{i}",
            "name": name,
            "email": str(contact.get("EmailAddress") or "").strip() or None,
            "reference": clean_reference(
                str(bill.get("Reference") or bill.get("InvoiceNumber") or "")
            ) or None,
            "receiver_type": receiver_type(name),
            "amount": to_float(bill.get("AmountDue")),
            "amount_currency": cfg["amount_currency"],
            "source_currency": currency,
            "target_currency": currency,
            "due_date": due.isoformat() if due else None,
            "matched": matched,
            **details,
        })
    return rows


def _consolidate_by_vendor(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per vendor, amounts summed, references joined.

    US only.  Order of first appearance is preserved so the output is stable
    run to run.
    """
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = row["name"].lower()
        if key not in grouped:
            grouped[key] = {**row, "_refs": []}
            order.append(key)
        else:
            grouped[key]["amount"] += row["amount"]
        if row.get("reference"):
            grouped[key]["_refs"].append(row["reference"])

    out: list[dict[str, Any]] = []
    for key in order:
        row = grouped.pop(key)
        refs = dict.fromkeys(row.pop("_refs"))  # de-duplicate, keep order
        row["reference"] = ", ".join(refs) or None
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Phase 3 — finalise
# ---------------------------------------------------------------------------

def finalise(
    params: dict[str, Any],
    rows: list[dict[str, Any]],
    ctx: RunContext,
    columns: list[str],
) -> RunResult:
    """Turn the approved rows into a bank-ready CSV.

    `columns` is the org's bank-file layout, read once from their template at
    onboarding.  Values come from the row where the column name matches a
    banking field or a bill field; anything else is left blank rather than
    guessed, so a layout change shows up as an empty column instead of
    silently shifted data.
    """
    import csv
    import io

    from ..banking import norm_header

    region = params["region"]
    cfg = REGIONS[region]

    bill_fields = {
        "name": "name",
        "recipientemail": "email",
        "paymentreference": "reference",
        "receivertype": "receiver_type",
        "amountcurrency": "amount_currency",
        "amount": "amount",
        "sourcecurrency": "source_currency",
        "targetcurrency": "target_currency",
    }
    defaults = {"accounttype": "CHECKING", "addresscountrycode": "US", "type": ""}

    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(columns)

    for row in rows:
        out = []
        for col in columns:
            key = norm_header(col)
            if key in bill_fields:
                out.append(row.get(bill_fields[key], ""))
            elif key in row:
                out.append(row.get(key, ""))
            else:
                out.append(defaults.get(key, ""))
        writer.writerow(out)

    total = sum(to_float(r.get("amount")) for r in rows)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return RunResult(
        status=RunStatus.COMPLETE,
        rows=rows,
        summary=(
            f"{len(rows)} payment(s), {cfg['currency']} {total:,.2f} — "
            f"file ready to upload to the bank."
        ),
        artifact_name=f"payrun-{region}-{stamp}.csv",
        artifact_bytes=buf.getvalue().encode("utf-8-sig"),
    )
