"""Seed a dev org, and import an existing desktop setup into it.

This doubles as the onboarding path a real customer takes, minus the UI:
create an org, import vendor bank details and the bank-file layout from the
spreadsheet they already maintain, and (dev only) carry across an existing
Xero token so the org is immediately usable.

    python -m fw.seed --email peter@funraisin.co --org funraisin

Importing the desktop Xero token is gated behind --import-token and only
works against SQLite: a token minted for the desktop app's own Xero client
would not refresh under the platform's client anyway, so it is a local
convenience, not a migration strategy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from . import env
from .banking import ExcelBankDetails, payment_columns
from .crypto import Cipher
from .db import create_db_engine, init_db, is_sqlite, org_members, orgs
from .stores import (
    ConnectionStore,
    LayoutStore,
    OrgStore,
    VendorStore,
    WorkflowAccessStore,
)

DESKTOP_DIR = (
    Path.home() / "workspace" / "local-desktop-apps" / "xero-bill-exporter"
)
TEMPLATE_KEY = {
    "UK": "template_path",
    "US": "template_path_us",
    "EU": "template_path_eur",
}


def _desktop_config() -> dict:
    path = Path(os.environ.get("FW_DESKTOP_CONFIG", DESKTOP_DIR / "config.json"))
    if not path.exists():
        print(f"  ! desktop config not found at {path} — skipping import")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_org(engine, name: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(orgs.c.id).where(orgs.c.name == name)).first()
    return str(row.id) if row else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--org", default="funraisin")
    parser.add_argument(
        "--user-id",
        default=os.environ.get("FW_DEV_USER_ID",
                               "00000000-0000-0000-0000-000000000001"),
        help="Supabase auth.users id for this member.",
    )
    parser.add_argument("--import-token", action="store_true",
                        help="Copy the desktop Xero token in (SQLite only).")
    args = parser.parse_args(argv)
    env.load()

    engine = create_db_engine()
    init_db(engine)

    org_id = _existing_org(engine, args.org)
    if org_id:
        print(f"org '{args.org}' already exists ({org_id})")
        with engine.begin() as conn:
            member = conn.execute(
                select(org_members.c.user_id)
                .where(org_members.c.org_id == org_id)
                .where(org_members.c.user_id == args.user_id)
            ).first()
            if member is None:
                from sqlalchemy import insert

                conn.execute(insert(org_members).values(
                    org_id=org_id, user_id=args.user_id,
                    email=args.email, role="admin",
                ))
                print(f"  + added {args.email} as admin")
    else:
        org_id = OrgStore(engine).create(args.org, args.user_id, args.email)
        print(f"created org '{args.org}' ({org_id}) with {args.email} as admin")

    # Workflow access fails closed, so a seeded org must be granted explicitly.
    # The pay run is what this script sets up data for.
    WorkflowAccessStore(engine).grant(org_id, "weekly-payrun", args.email)
    print("  + workflow access: weekly-payrun")

    cfg = _desktop_config()
    layouts = LayoutStore(engine)
    for region, key in TEMPLATE_KEY.items():
        raw = cfg.get(key, "")
        if not raw or not Path(raw).exists():
            print(f"  - {region}: no template configured, skipped")
            continue
        excel = ExcelBankDetails(raw)
        count = VendorStore.replace_region(
            engine, org_id, region, excel.as_records()
        )
        columns = payment_columns(raw)
        layouts.set(org_id, region, columns)
        print(f"  + {region}: {count} vendor(s), {len(columns)} bank-file column(s)")

    if args.import_token:
        if not is_sqlite():
            print("  ! --import-token refused: only supported on SQLite")
            return 1
        token_dir = Path(
            os.environ.get("FW_TOKEN_DIR",
                           Path(os.environ.get("APPDATA", Path.home()))
                           / "xero-bill-exporter")
        )
        store = ConnectionStore(engine, Cipher())
        for name, filename in (("default", "token.json"), ("us", "token_us.json")):
            path = token_dir / filename
            if not path.exists():
                continue
            token = json.loads(path.read_text(encoding="utf-8"))
            token["connected_by"] = args.email
            store.save(org_id, name, token)
            print(f"  + imported Xero token '{name}' "
                  f"({token.get('tenant_name') or 'unknown org'}) — encrypted")

    print(f"\nFW_DEV_USER={args.email}")
    print(f"FW_DEV_USER_ID={args.user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
