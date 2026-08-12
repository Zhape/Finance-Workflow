"""Per-org Xero applications.

The property that matters: one org's client credentials are never visible to
another, and the platform default only applies when an org has none of its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw import oauth  # noqa: E402
from fw.crypto import Cipher, generate_key  # noqa: E402
from fw.db import create_db_engine, init_db  # noqa: E402
from fw.db import provider_apps as apps_table  # noqa: E402
from fw.stores import OrgStore, ProviderAppStore  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"
BOB = "00000000-0000-0000-0000-0000000000b1"


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'apps.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def two_orgs(engine):
    store = OrgStore(engine)
    return (store.create("Acme", ALICE, "alice@acme.test"),
            store.create("Globex", BOB, "bob@globex.test"))


@pytest.fixture
def apps(engine):
    return ProviderAppStore(engine, Cipher(key=generate_key()))


def test_app_credentials_are_org_scoped(engine, two_orgs, apps):
    acme, globex = two_orgs
    apps.save(acme, "ACME-CLIENT", "acme-secret", "alice@acme.test")

    assert apps.get(acme) == ("ACME-CLIENT", "acme-secret")
    assert apps.get(globex) is None


def test_client_secret_is_not_stored_in_plaintext(engine, two_orgs, apps):
    acme, _ = two_orgs
    apps.save(acme, "ACME-CLIENT", "TOPSECRET", "alice@acme.test")

    with engine.connect() as conn:
        raw = conn.execute(select(apps_table.c.secret)).scalar_one()
    assert b"TOPSECRET" not in bytes(raw)


def test_status_shows_the_client_id_but_never_the_secret(engine, two_orgs, apps):
    acme, _ = two_orgs
    apps.save(acme, "ACME-CLIENT", "TOPSECRET", "alice@acme.test", label="Acme app")

    status = apps.status(acme)
    assert status["source"] == "org"
    assert status["clientId"] == "ACME-CLIENT"
    assert "TOPSECRET" not in json.dumps(status)


def test_status_reports_platform_when_the_org_has_no_app(engine, two_orgs, apps):
    _, globex = two_orgs
    assert apps.status(globex) == {"source": "platform", "clientId": None}


def test_saving_twice_replaces_rather_than_duplicates(engine, two_orgs, apps):
    acme, _ = two_orgs
    apps.save(acme, "FIRST", "s1", "alice@acme.test")
    apps.save(acme, "SECOND", "s2", "alice@acme.test")

    assert apps.get(acme) == ("SECOND", "s2")
    with engine.connect() as conn:
        assert conn.execute(select(apps_table.c.org_id)).all().__len__() == 1


def test_clear_is_org_scoped(engine, two_orgs, apps):
    acme, globex = two_orgs
    apps.save(acme, "ACME-CLIENT", "s", "alice@acme.test")

    assert apps.clear(globex) is False
    assert apps.get(acme) is not None
    assert apps.clear(acme) is True
    assert apps.get(acme) is None


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_org_app_wins_over_the_platform(engine, two_orgs, apps, monkeypatch):
    acme, _ = two_orgs
    monkeypatch.setenv("FW_XERO_CLIENT_ID", "PLATFORM")
    monkeypatch.setenv("FW_XERO_CLIENT_SECRET", "platform-secret")
    apps.save(acme, "ORG-CLIENT", "org-secret", "alice@acme.test")

    client_id, secret, _redirect = oauth._client(acme, apps)
    assert (client_id, secret) == ("ORG-CLIENT", "org-secret")


def test_platform_app_is_the_fallback(engine, two_orgs, apps, monkeypatch):
    _, globex = two_orgs
    monkeypatch.setenv("FW_XERO_CLIENT_ID", "PLATFORM")
    monkeypatch.setenv("FW_XERO_CLIENT_SECRET", "platform-secret")

    client_id, secret, _redirect = oauth._client(globex, apps)
    assert (client_id, secret) == ("PLATFORM", "platform-secret")


def test_no_app_anywhere_is_a_clear_error(engine, two_orgs, apps, monkeypatch):
    _, globex = two_orgs
    monkeypatch.delenv("FW_XERO_CLIENT_ID", raising=False)
    monkeypatch.delenv("FW_XERO_CLIENT_SECRET", raising=False)

    with pytest.raises(oauth.OAuthError) as exc:
        oauth._client(globex, apps)
    assert "Settings" in str(exc.value)


def test_one_org_cannot_resolve_anothers_app(engine, two_orgs, apps, monkeypatch):
    """The fallback must be the platform, never some other org's credentials."""
    acme, globex = two_orgs
    monkeypatch.setenv("FW_XERO_CLIENT_ID", "PLATFORM")
    monkeypatch.setenv("FW_XERO_CLIENT_SECRET", "platform-secret")
    apps.save(acme, "ACME-CLIENT", "acme-secret", "alice@acme.test")

    client_id, _secret, _r = oauth._client(globex, apps)
    assert client_id == "PLATFORM"
