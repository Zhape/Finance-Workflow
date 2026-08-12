"""Tests for the bank-details sources.

The Excel source exists only for onboarding; the invariant that matters is
that what it exports matches what the table source expects, because the
workflow only ever sees the table.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook  # noqa: E402

from fw.banking import ExcelBankDetails, TableBankDetails, payment_columns  # noqa: E402


def make_template(tmp_path: Path) -> str:
    """A bank template shaped like the real ones: payment sheet + mapping sheet,
    with trailing blank header cells on the mapping sheet."""
    wb = Workbook()
    payment = wb.active
    payment.title = "Payment"
    payment.append(["name", "amount", "sortCode", "accountNumber"])

    mapping = wb.create_sheet("Mapping")
    mapping.append(["Vendor", "Sort Code", "Account Number", None, None])
    mapping.append(["Acme Ltd", "20-00-00", 123456, None, None])
    mapping.append([None, None, None, None, None])  # blank filler row
    mapping.append(["Jane Smith", "30-00-00", "87654321", None, None])

    path = tmp_path / "template.xlsx"
    wb.save(path)
    return str(path)


def test_excel_ignores_blank_header_columns(tmp_path):
    src = ExcelBankDetails(make_template(tmp_path))
    assert src.field_keys() == ["sortcode", "accountnumber"]


def test_excel_skips_rows_with_no_vendor(tmp_path):
    src = ExcelBankDetails(make_template(tmp_path))
    assert src.lookup("") == {}
    assert src.lookup("Acme Ltd")["sortcode"] == "20-00-00"


def test_excel_export_round_trips_into_the_table_source(tmp_path):
    """Onboarding invariant: template -> records -> table must be lossless,
    and must not invent columns from blank header cells."""
    excel = ExcelBankDetails(make_template(tmp_path))
    table = TableBankDetails(excel.as_records())

    assert table.field_keys() == excel.field_keys()
    for vendor in ("Acme Ltd", "Jane Smith"):
        assert table.lookup(vendor) == excel.lookup(vendor)


def test_excel_zero_pads_account_numbers(tmp_path):
    src = ExcelBankDetails(make_template(tmp_path))
    assert src.lookup("Acme Ltd")["accountnumber"] == "00123456"


def test_payment_columns_come_from_the_non_mapping_sheet(tmp_path):
    assert payment_columns(make_template(tmp_path)) == [
        "name", "amount", "sortCode", "accountNumber",
    ]


def test_unknown_vendor_is_empty_not_an_error(tmp_path):
    src = ExcelBankDetails(make_template(tmp_path))
    assert src.lookup("Nobody Ltd") == {}
