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
    status = apps.status(globex)
    # Asserted by meaning rather than by exact shape. The contract is "this org
    # rides the platform application and has none of its own"; pinning the whole
    # dict turns every added field into a false failure.
    assert status["source"] == "platform"
    assert status["clientId"] is None
    assert status["apps"] == []


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


# ---------------------------------------------------------------------------
# Providers are independent
# ---------------------------------------------------------------------------

def test_xero_and_google_apps_are_separate_for_the_same_org(engine, two_orgs):
    """One org, two providers, two applications. Registering a Google client
    must not disturb the Xero one -- they are different rows, and conflating
    them would silently break Xero the moment someone connected Gmail."""
    from fw.crypto import Cipher, generate_key
    from fw.stores import ProviderAppStore

    acme, _ = two_orgs
    key = generate_key()
    xero = ProviderAppStore(engine, Cipher(key=key), provider="xero")
    google = ProviderAppStore(engine, Cipher(key=key), provider="google")

    xero.save(acme, "XERO-CLIENT", "xero-secret", "alice@acme.test")
    google.save(acme, "GOOGLE-CLIENT", "google-secret", "alice@acme.test")

    assert xero.get(acme) == ("XERO-CLIENT", "xero-secret")
    assert google.get(acme) == ("GOOGLE-CLIENT", "google-secret")

    # Reverting one provider leaves the other untouched.
    assert google.clear(acme) is True
    assert google.get(acme) is None
    assert xero.get(acme) == ("XERO-CLIENT", "xero-secret")


def test_google_app_resolution_falls_back_to_the_platform(engine, two_orgs,
                                                          monkeypatch):
    from fw import google as g
    from fw.crypto import Cipher, generate_key
    from fw.stores import ProviderAppStore

    acme, globex = two_orgs
    apps = ProviderAppStore(engine, Cipher(key=generate_key()), provider="google")
    monkeypatch.setenv("FW_GOOGLE_CLIENT_ID", "PLATFORM-GOOGLE")
    monkeypatch.setenv("FW_GOOGLE_CLIENT_SECRET", "platform-secret")
    apps.save(acme, "ACME-GOOGLE", "acme-secret", "alice@acme.test")

    assert g._client(acme, apps)[0] == "ACME-GOOGLE"
    assert g._client(globex, apps)[0] == "PLATFORM-GOOGLE"


# ---------------------------------------------------------------------------
# Redirect URIs
# ---------------------------------------------------------------------------

def test_a_localhost_redirect_is_refused_on_a_public_host(engine, two_orgs,
                                                          monkeypatch):
    """The default is localhost so local development needs no configuration.
    On a deployed host that default silently produces redirect_uri_mismatch,
    and the provider's error names neither the variable nor the service."""
    from fw import google as g
    from fw.env import redirect_warning

    monkeypatch.setenv("FW_ENV", "production")
    monkeypatch.delenv("FW_GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.setenv("FW_GOOGLE_CLIENT_ID", "PLATFORM")
    monkeypatch.setenv("FW_GOOGLE_CLIENT_SECRET", "secret")

    warning = redirect_warning(g.redirect_uri(), "FW_GOOGLE_REDIRECT_URI")
    assert warning and "FW_GOOGLE_REDIRECT_URI" in warning

    class Store:
        def put(self, *a, **k):
            raise AssertionError("consent must not be started")

    with pytest.raises(g.GoogleError) as exc:
        g.start(Store(), two_orgs[0], "default", "alice@acme.test")
    assert "FW_GOOGLE_REDIRECT_URI" in str(exc.value)


def test_localhost_is_fine_in_development(monkeypatch):
    from fw.env import redirect_warning

    monkeypatch.setenv("FW_ENV", "dev")
    assert redirect_warning(
        "http://localhost:8000/api/connections/google/callback",
        "FW_GOOGLE_REDIRECT_URI") is None


def test_a_public_redirect_passes(monkeypatch):
    from fw.env import redirect_warning

    monkeypatch.setenv("FW_ENV", "production")
    assert redirect_warning(
        "https://worker.example.com/api/connections/google/callback",
        "FW_GOOGLE_REDIRECT_URI") is None


def test_the_same_guard_covers_xero(engine, two_orgs, monkeypatch):
    from fw import oauth as o

    monkeypatch.setenv("FW_ENV", "production")
    monkeypatch.delenv("FW_XERO_REDIRECT_URI", raising=False)
    monkeypatch.setenv("FW_XERO_CLIENT_ID", "PLATFORM")

    class Store:
        def put(self, *a, **k):
            raise AssertionError("consent must not be started")

    with pytest.raises(o.OAuthError) as exc:
        o.start(Store(), two_orgs[0], "default", "alice@acme.test")
    assert "FW_XERO_REDIRECT_URI" in str(exc.value)
