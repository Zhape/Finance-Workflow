"""Workflow access is per-org, and fails closed.

The property worth defending: a new org has nothing until granted, and one
org's grants never leak into another's catalogue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.db import create_db_engine, init_db  # noqa: E402
from fw.stores import OrgStore, WorkflowAccessStore  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"
BOB = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'access.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def orgs(engine):
    store = OrgStore(engine)
    return (store.create("Acme", ALICE, "alice@acme.test"),
            store.create("Globex", BOB, "bob@globex.test"))


@pytest.fixture
def access(engine):
    return WorkflowAccessStore(engine)


def test_a_new_org_has_no_workflows(engine, orgs, access):
    """Fails closed. An org that was never granted anything sees nothing."""
    acme, _ = orgs
    assert access.keys(acme) == set()
    assert access.has(acme, "weekly-payrun") is False


def test_grants_are_org_scoped(engine, orgs, access):
    acme, globex = orgs
    access.grant(acme, "weekly-payrun", "alice@acme.test")
    access.grant(globex, "overdue-chaseup", "bob@globex.test")

    assert access.keys(acme) == {"weekly-payrun"}
    assert access.keys(globex) == {"overdue-chaseup"}
    assert access.has(globex, "weekly-payrun") is False


def test_granting_twice_is_harmless(engine, orgs, access):
    acme, _ = orgs
    access.grant(acme, "weekly-payrun", "alice@acme.test")
    access.grant(acme, "weekly-payrun", "someone.else@acme.test")
    assert access.keys(acme) == {"weekly-payrun"}


def test_revoke_is_org_scoped(engine, orgs, access):
    acme, globex = orgs
    access.grant(acme, "weekly-payrun", "alice@acme.test")

    assert access.revoke(globex, "weekly-payrun") is False
    assert access.has(acme, "weekly-payrun") is True
    assert access.revoke(acme, "weekly-payrun") is True
    assert access.keys(acme) == set()


def test_revoking_something_never_granted_reports_false(engine, orgs, access):
    acme, _ = orgs
    assert access.revoke(acme, "never-granted") is False


def test_an_org_can_hold_several_workflows(engine, orgs, access):
    acme, _ = orgs
    access.grant(acme, "weekly-payrun", "alice@acme.test")
    access.grant(acme, "overdue-chaseup", "alice@acme.test")
    assert access.keys(acme) == {"weekly-payrun", "overdue-chaseup"}
