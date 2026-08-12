"""Several OAuth applications per org, and a connection per Xero organisation.

Xero forced both. An unpublished app may hold at most two organisations, so a
customer with three needs two apps; and a refresh token is bound to the client
that issued it, so every connection has to remember which app minted it or the
refresh quietly uses the wrong credentials and fails mid-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.crypto import Cipher, generate_key  # noqa: E402
from fw.db import create_db_engine, init_db  # noqa: E402
from fw.stores import ConnectionStore, OrgStore, ProviderAppStore  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"
BOB = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'apps.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def cipher():
    return Cipher(key=generate_key())


@pytest.fixture
def orgs(engine):
    store = OrgStore(engine)
    return (store.create("Acme", ALICE, "alice@acme.test"),
            store.create("Globex", BOB, "bob@globex.test"))


@pytest.fixture
def apps(engine, cipher):
    return ProviderAppStore(engine, cipher, provider="xero")


@pytest.fixture
def conns(engine, cipher):
    return ConnectionStore(engine, cipher, provider="xero")


def connect(conns, org_id, name, *, tenant_id, tenant_name, app_name):
    conns.save(org_id, name, {
        "access_token": "a", "refresh_token": "r", "obtained_at": 0,
        "tenant_id": tenant_id, "tenant_name": tenant_name,
        "app_name": app_name, "connected_by": "alice@acme.test",
    })


# ---------------------------------------------------------------------------
# Several apps
# ---------------------------------------------------------------------------

def test_an_org_can_register_more_than_one_app(engine, orgs, apps):
    acme, _ = orgs
    apps.save(acme, "ID-UK", "s1", "alice@acme.test", label="UK app", name="uk")
    apps.save(acme, "ID-AU", "s2", "alice@acme.test", label="AU app", name="au")

    assert [a["name"] for a in apps.list(acme)] == ["au", "uk"]
    assert apps.get(acme, "uk") == ("ID-UK", "s1")
    assert apps.get(acme, "au") == ("ID-AU", "s2")


def test_saving_the_same_app_twice_replaces_it(engine, orgs, apps):
    acme, _ = orgs
    apps.save(acme, "FIRST", "s1", "alice@acme.test", name="uk")
    apps.save(acme, "SECOND", "s2", "alice@acme.test", name="uk")

    assert len(apps.list(acme)) == 1
    assert apps.get(acme, "uk") == ("SECOND", "s2")


def test_apps_are_org_scoped(engine, orgs, apps):
    acme, globex = orgs
    apps.save(acme, "ACME", "s", "alice@acme.test", name="uk")
    assert apps.list(globex) == []
    assert apps.get(globex, "uk") is None


def test_a_secret_is_never_returned_by_the_listing(engine, orgs, apps):
    import json

    acme, _ = orgs
    apps.save(acme, "ID", "TOPSECRET", "alice@acme.test", name="uk")
    assert "TOPSECRET" not in json.dumps(apps.list(acme))


# ---------------------------------------------------------------------------
# Capacity — Xero allows an unpublished app two organisations
# ---------------------------------------------------------------------------

def test_capacity_is_counted_in_organisations_not_rows(engine, orgs, apps, conns):
    acme, _ = orgs
    apps.save(acme, "ID", "s", "alice@acme.test", name="uk")
    connect(conns, acme, "one", tenant_id="t1", tenant_name="One", app_name="uk")

    assert apps.list(acme)[0]["full"] is False

    connect(conns, acme, "two", tenant_id="t2", tenant_name="Two", app_name="uk")
    assert apps.list(acme)[0]["full"] is True, \
        "two organisations is the ceiling for an unpublished Xero app"


def test_reconnecting_the_same_organisation_does_not_consume_a_second_slot(
        engine, orgs, apps, conns):
    acme, _ = orgs
    apps.save(acme, "ID", "s", "alice@acme.test", name="uk")
    connect(conns, acme, "one", tenant_id="t1", tenant_name="One", app_name="uk")
    connect(conns, acme, "one-again", tenant_id="t1", tenant_name="One",
            app_name="uk")

    assert apps.list(acme)[0]["organisations"] == ["t1"]
    assert apps.list(acme)[0]["full"] is False


def test_each_app_has_its_own_capacity(engine, orgs, apps, conns):
    acme, _ = orgs
    apps.save(acme, "ID-UK", "s", "alice@acme.test", name="uk")
    apps.save(acme, "ID-AU", "s", "alice@acme.test", name="au")
    connect(conns, acme, "one", tenant_id="t1", tenant_name="One", app_name="uk")
    connect(conns, acme, "two", tenant_id="t2", tenant_name="Two", app_name="uk")

    by_name = {a["name"]: a for a in apps.list(acme)}
    assert by_name["uk"]["full"] is True
    assert by_name["au"]["full"] is False, "a sibling app has its own ceiling"


# ---------------------------------------------------------------------------
# A token belongs to the app that issued it
# ---------------------------------------------------------------------------

def test_a_connection_remembers_which_app_issued_it(engine, orgs, conns):
    acme, _ = orgs
    connect(conns, acme, "uk-org", tenant_id="t1", tenant_name="UK Ltd",
            app_name="uk")

    assert conns.load(acme, "uk-org")["app_name"] == "uk"
    assert conns.list(acme, provider="xero")[0]["appName"] == "uk"


def test_apps_in_use_reports_dependent_connections(engine, orgs, apps, conns):
    acme, _ = orgs
    apps.save(acme, "ID", "s", "alice@acme.test", name="uk")
    assert apps.in_use(acme, "uk") == 0

    connect(conns, acme, "one", tenant_id="t1", tenant_name="One", app_name="uk")
    assert apps.in_use(acme, "uk") == 1


# ---------------------------------------------------------------------------
# Connections named from Xero
# ---------------------------------------------------------------------------

def test_a_connection_is_labelled_with_its_xero_organisation_name(
        engine, orgs, conns):
    """'XERO — US' described a slot. The label should describe the company."""
    acme, _ = orgs
    connect(conns, acme, "funraisin-limited-uk", tenant_id="t1",
            tenant_name="Funraisin Limited (UK)", app_name="uk")

    row = conns.list(acme, provider="xero")[0]
    assert row["label"] == "Funraisin Limited (UK)"
    assert row["tenantId"] == "t1"


def test_connections_are_filtered_by_provider(engine, cipher, orgs):
    """A Google connection called 'default' must not answer for Xero's.

    This exact collision made the settings screen report Xero connected while
    the inbox correctly reported it was not.
    """
    acme, _ = orgs
    google = ConnectionStore(engine, cipher, provider="google")
    google.save(acme, "default", {"access_token": "a", "refresh_token": "r"})

    xero = ConnectionStore(engine, cipher, provider="xero")
    assert xero.list(acme, provider="xero") == []
    assert len(xero.list(acme)) == 1, "unfiltered still returns everything"


# ---------------------------------------------------------------------------
# The workflow parameter the platform fills in
# ---------------------------------------------------------------------------

def test_setup_status_is_callable_for_both_providers(engine, orgs, apps, cipher):
    """A smoke test, added because it was missing.

    Threading an app name through `_client` left `setup_status` referring to a
    variable that did not exist there. Nothing failed until a browser loaded
    the settings page, because no test had ever called this function.
    """
    from fw import google, oauth

    acme, _ = orgs
    apps.save(acme, "ID", "s", "alice@acme.test", name="uk")

    xero_status = oauth.setup_status(acme, apps)
    assert xero_status["appConfigured"] is True
    assert xero_status["app"]["source"] == "org"

    google_apps = ProviderAppStore(engine, cipher, provider="google")
    google_status = google.setup_status(acme, google_apps)
    assert "appConfigured" in google_status
    assert google_status["app"]["source"] == "platform"


def test_setup_status_survives_an_org_with_no_app(engine, orgs, apps):
    from fw import oauth

    _, globex = orgs
    status = oauth.setup_status(globex, apps)
    assert status["app"]["source"] == "platform"


def test_a_choice_with_no_options_accepts_the_orgs_own_value():
    """Workflows declare `options=[]` for the Xero organisation because they
    cannot know a customer's estate. Validating against [] rejected everything."""
    from fw.contract import ParamSpec

    spec = ParamSpec(name="connection", type="choice", label="Xero organisation",
                     required=False, options=[], default=None)
    assert spec.validate("funraisin-limited-uk") == "funraisin-limited-uk"


def test_a_choice_with_options_still_rejects_an_unknown_value():
    from fw.contract import ParamSpec

    spec = ParamSpec(name="region", type="choice", label="Region",
                     options=["UK", "US"], default="UK")
    with pytest.raises(ValueError):
        spec.validate("MARS")
