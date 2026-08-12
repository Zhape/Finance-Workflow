"""The dev principal must fail closed.

A host that forgets FW_ENV must reject unauthenticated callers, not treat them
as a signed-in member. This is the difference between a missing environment
variable being a nuisance and being a data breach.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.auth import AuthError, principal_from_request  # noqa: E402
from fw.db import create_db_engine, init_db  # noqa: E402
from fw.stores import OrgStore  # noqa: E402

USER = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def engine_with_member(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    init_db(eng)
    OrgStore(eng).create("Acme", USER, "alice@acme.test")
    return eng


def test_unset_fw_env_rejects_anonymous_callers(engine_with_member, monkeypatch):
    monkeypatch.delenv("FW_ENV", raising=False)
    monkeypatch.setenv("FW_DEV_USER", "alice@acme.test")
    monkeypatch.setenv("FW_DEV_USER_ID", USER)

    with pytest.raises(AuthError) as exc:
        principal_from_request(engine_with_member, None, None)
    assert exc.value.status == 401


def test_production_fw_env_rejects_anonymous_callers(engine_with_member, monkeypatch):
    monkeypatch.setenv("FW_ENV", "production")
    monkeypatch.setenv("FW_DEV_USER", "alice@acme.test")
    monkeypatch.setenv("FW_DEV_USER_ID", USER)

    with pytest.raises(AuthError):
        principal_from_request(engine_with_member, None, None)


def test_dev_env_allows_the_dev_principal(engine_with_member, monkeypatch):
    monkeypatch.setenv("FW_ENV", "dev")
    monkeypatch.setenv("FW_DEV_USER", "alice@acme.test")
    monkeypatch.setenv("FW_DEV_USER_ID", USER)

    principal = principal_from_request(engine_with_member, None, None)
    assert principal.email == "alice@acme.test"
    assert principal.role == "admin"


def test_a_bad_authorization_header_is_never_treated_as_dev(engine_with_member,
                                                            monkeypatch):
    """Presenting a broken token must not fall back to the dev principal."""
    monkeypatch.setenv("FW_ENV", "dev")
    monkeypatch.setenv("FW_DEV_USER", "alice@acme.test")

    with pytest.raises(AuthError):
        principal_from_request(engine_with_member, "Bearer not-a-jwt", None)
