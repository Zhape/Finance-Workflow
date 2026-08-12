"""Tenancy, credential storage and role tests.

These are the tests that matter most for a multi-tenant product: everything
here is about one org being unable to reach another's data, and about stored
tokens being useless to anyone who reads the table.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.auth import AuthError, Principal, _resolve_membership  # noqa: E402
from fw.contract import Column, RunResult, RunStatus  # noqa: E402
from fw.crypto import Cipher, EncryptionError, generate_key  # noqa: E402
from fw.db import connections as connections_table  # noqa: E402
from fw.db import create_db_engine, init_db  # noqa: E402
from fw.stores import (  # noqa: E402
    ConnectionStore,
    LayoutStore,
    OAuthStateStore,
    OrgStore,
    RunStore,
    VendorStore,
)

ALICE = "00000000-0000-0000-0000-0000000000a1"
BOB = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def cipher():
    return Cipher(key=generate_key())


@pytest.fixture
def two_orgs(engine):
    store = OrgStore(engine)
    acme = store.create("Acme", ALICE, "alice@acme.test")
    globex = store.create("Globex", BOB, "bob@globex.test")
    return acme, globex


def _result(rows, name="payrun.csv"):
    return RunResult(
        status=RunStatus.COMPLETE, columns=[Column("name", "Vendor")],
        rows=rows, summary="done", artifact_name=name,
        artifact_bytes=b"name\nAcme Ltd\n",
    )


# ---------------------------------------------------------------------------
# Run isolation
# ---------------------------------------------------------------------------

def test_runs_are_invisible_to_another_org(engine, two_orgs):
    acme, globex = two_orgs
    runs = RunStore(engine)
    run_id = runs.create(acme, "weekly-payrun", {"region": "UK"}, "alice@acme.test")

    assert runs.get(acme, run_id) is not None
    assert runs.get(globex, run_id) is None


def test_run_list_does_not_leak_across_orgs(engine, two_orgs):
    acme, globex = two_orgs
    runs = RunStore(engine)
    runs.create(acme, "weekly-payrun", {"region": "UK"}, "alice@acme.test")
    runs.create(globex, "weekly-payrun", {"region": "US"}, "bob@globex.test")

    assert [r["params"]["region"] for r in runs.list(acme)] == ["UK"]
    assert [r["params"]["region"] for r in runs.list(globex)] == ["US"]


def test_bank_file_cannot_be_downloaded_by_another_org(engine, two_orgs):
    """The artifact holds account numbers — the tightest thing in the system."""
    acme, globex = two_orgs
    runs = RunStore(engine)
    run_id = runs.create(acme, "weekly-payrun", {"region": "UK"}, "alice@acme.test")
    runs.complete(acme, run_id, _result([{"id": "1"}]), ["1"], "alice@acme.test")

    assert runs.artifact(acme, run_id)[0] == "payrun.csv"
    assert runs.artifact(globex, run_id) is None


def test_another_org_cannot_complete_your_run(engine, two_orgs):
    acme, globex = two_orgs
    runs = RunStore(engine)
    run_id = runs.create(acme, "weekly-payrun", {"region": "UK"}, "alice@acme.test")

    runs.complete(globex, run_id, _result([{"id": "1"}]), ["1"], "bob@globex.test")

    run = runs.get(acme, run_id)
    assert run["status"] == "needs_approval"
    assert run["approvedBy"] is None


def test_approval_is_attributed(engine, two_orgs):
    acme, _ = two_orgs
    runs = RunStore(engine)
    run_id = runs.create(acme, "weekly-payrun", {"region": "UK"}, "alice@acme.test")
    runs.complete(acme, run_id, _result([{"id": "1"}]), ["1"], "approver@acme.test")

    run = runs.get(acme, run_id)
    assert run["createdBy"] == "alice@acme.test"
    assert run["approvedBy"] == "approver@acme.test"
    assert run["approvedAt"] is not None


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def test_token_is_not_stored_in_plaintext(engine, two_orgs, cipher):
    acme, _ = two_orgs
    store = ConnectionStore(engine, cipher)
    store.save(acme, "default", {"refresh_token": "SUPERSECRET",
                                 "access_token": "at", "tenant_id": "t1"})

    with engine.connect() as conn:
        raw = conn.execute(select(connections_table.c.secret)).scalar_one()

    assert b"SUPERSECRET" not in bytes(raw)
    assert store.load(acme, "default")["refresh_token"] == "SUPERSECRET"


def test_connections_are_org_scoped(engine, two_orgs, cipher):
    acme, globex = two_orgs
    store = ConnectionStore(engine, cipher)
    store.save(acme, "default", {"refresh_token": "acme-token"})

    assert store.load(globex, "default") is None


def test_connection_list_never_returns_the_secret(engine, two_orgs, cipher):
    acme, _ = two_orgs
    store = ConnectionStore(engine, cipher)
    store.save(acme, "default", {"refresh_token": "SUPERSECRET",
                                 "tenant_name": "Acme Trading"})

    listed = store.list(acme)
    assert listed[0]["tenantName"] == "Acme Trading"
    assert "SUPERSECRET" not in json.dumps(listed)
    assert not any("secret" in k.lower() for k in listed[0])


def test_wrong_key_cannot_decrypt(engine, two_orgs, cipher):
    acme, _ = two_orgs
    ConnectionStore(engine, cipher).save(acme, "default", {"refresh_token": "x"})

    other = ConnectionStore(engine, Cipher(key=generate_key()))
    with pytest.raises(EncryptionError):
        other.load(acme, "default")


def test_reconnecting_replaces_rather_than_duplicates(engine, two_orgs, cipher):
    acme, _ = two_orgs
    store = ConnectionStore(engine, cipher)
    store.save(acme, "default", {"refresh_token": "first"})
    store.save(acme, "default", {"refresh_token": "second"})

    assert len(store.list(acme)) == 1
    assert store.load(acme, "default")["refresh_token"] == "second"


def test_disconnect_is_org_scoped(engine, two_orgs, cipher):
    acme, globex = two_orgs
    store = ConnectionStore(engine, cipher)
    store.save(acme, "default", {"refresh_token": "x"})

    assert store.disconnect(globex, "default") is False
    assert store.load(acme, "default") is not None
    assert store.disconnect(acme, "default") is True


# ---------------------------------------------------------------------------
# OAuth state
# ---------------------------------------------------------------------------

def test_oauth_state_is_single_use(engine, two_orgs):
    acme, _ = two_orgs
    states = OAuthStateStore(engine)
    states.put("s1", acme, "xero", "default", "verifier-1", "alice@acme.test")

    assert states.take("s1")["verifier"] == "verifier-1"
    assert states.take("s1") is None


def test_unknown_oauth_state_is_rejected(engine):
    assert OAuthStateStore(engine).take("never-issued") is None


def test_expired_oauth_state_is_rejected(engine, two_orgs, monkeypatch):
    acme, _ = two_orgs
    states = OAuthStateStore(engine)
    monkeypatch.setattr(OAuthStateStore, "TTL", timedelta(seconds=-1))
    states.put("s2", acme, "xero", "default", "v", "alice@acme.test")
    assert states.take("s2") is None


def test_oauth_state_carries_its_own_org(engine, two_orgs):
    """The callback has no session — the org must come from the state row."""
    acme, _ = two_orgs
    states = OAuthStateStore(engine)
    states.put("s3", acme, "xero", "us", "v", "alice@acme.test")
    assert states.take("s3")["org_id"] == acme


# ---------------------------------------------------------------------------
# Vendor data
# ---------------------------------------------------------------------------

def test_vendor_lookup_is_org_scoped(engine, two_orgs):
    acme, globex = two_orgs
    VendorStore.replace_region(engine, acme, "UK", [
        {"vendor": "Acme Supplies", "Sort Code": "20-00-00",
         "Account Number": 123456},
    ])

    assert VendorStore(engine, acme, "UK").lookup("Acme Supplies")["sortcode"] \
        == "20-00-00"
    assert VendorStore(engine, globex, "UK").lookup("Acme Supplies") == {}


def test_vendor_import_replaces_rather_than_merges(engine, two_orgs):
    """A vendor removed at source must not survive as a stale account number."""
    acme, _ = two_orgs
    VendorStore.replace_region(engine, acme, "UK", [
        {"vendor": "Keep Ltd", "Sort Code": "20-00-00"},
        {"vendor": "Remove Ltd", "Sort Code": "30-00-00"},
    ])
    VendorStore.replace_region(engine, acme, "UK", [
        {"vendor": "Keep Ltd", "Sort Code": "20-00-00"},
    ])

    store = VendorStore(engine, acme, "UK")
    assert store.lookup("Keep Ltd")["sortcode"] == "20-00-00"
    assert store.lookup("Remove Ltd") == {}


def test_vendor_regions_are_independent(engine, two_orgs):
    acme, _ = two_orgs
    VendorStore.replace_region(engine, acme, "UK", [{"vendor": "A", "Sort Code": "1"}])
    VendorStore.replace_region(engine, acme, "US", [{"vendor": "A", "abartn": "2"}])

    assert VendorStore(engine, acme, "UK").lookup("A") == {"sortcode": "1"}
    assert VendorStore(engine, acme, "US").lookup("A") == {"abartn": "2"}


def test_layouts_are_org_scoped(engine, two_orgs):
    acme, globex = two_orgs
    layouts = LayoutStore(engine)
    layouts.set(acme, "UK", ["name", "amount"])

    assert layouts.get(acme, "UK") == ["name", "amount"]
    assert layouts.get(globex, "UK") is None


# ---------------------------------------------------------------------------
# Identity and roles
# ---------------------------------------------------------------------------

def test_membership_resolves_to_the_users_org(engine, two_orgs):
    acme, _ = two_orgs
    principal = _resolve_membership(engine, ALICE, "alice@acme.test", None)
    assert principal.org_id == acme
    assert principal.role == "admin"


def test_org_hint_for_a_non_member_org_is_refused(engine, two_orgs):
    """A member of Acme asking to act as Globex must be refused, and must not
    learn whether Globex exists."""
    _acme, globex = two_orgs
    with pytest.raises(AuthError) as exc:
        _resolve_membership(engine, ALICE, "alice@acme.test", globex)
    assert exc.value.status == 404
    assert "not found" in str(exc.value).lower()


def test_unknown_org_hint_looks_identical_to_a_forbidden_one(engine, two_orgs):
    with pytest.raises(AuthError) as unknown:
        _resolve_membership(engine, ALICE, "alice@acme.test", "no-such-org")
    with pytest.raises(AuthError) as forbidden:
        _resolve_membership(engine, ALICE, "alice@acme.test", two_orgs[1])
    assert str(unknown.value) == str(forbidden.value)
    assert unknown.value.status == forbidden.value.status


def test_user_with_no_membership_is_refused(engine, two_orgs):
    with pytest.raises(AuthError) as exc:
        _resolve_membership(engine, "00000000-0000-0000-0000-0000000000ff",
                            "nobody@nowhere.test", None)
    assert exc.value.status == 403


@pytest.mark.parametrize("role,minimum,allowed", [
    ("admin", "admin", True),
    ("member", "admin", False),
    ("member", "member", True),
    ("viewer", "member", False),
    ("viewer", "viewer", True),
])
def test_role_requirements(role, minimum, allowed):
    principal = Principal("u", "u@x.test", "org", "Org", role)
    if allowed:
        principal.require(minimum)
    else:
        with pytest.raises(AuthError) as exc:
            principal.require(minimum)
        assert exc.value.status == 403


def test_viewer_cannot_approve_a_payment_run():
    """The role gate that keeps a read-only user from releasing money."""
    viewer = Principal("u", "v@x.test", "org", "Org", "viewer")
    with pytest.raises(AuthError):
        viewer.require("member")
