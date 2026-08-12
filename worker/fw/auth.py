"""Who is calling, and which org are they calling for.

Supabase Auth issues the JWT; this module verifies it and then resolves the
caller to a membership row. Both halves matter: a valid token proves identity,
but it says nothing about which org's data the caller may touch. That comes
only from `org_members`, never from a header or a claim the client controls.

Verification modes, in order:
  1. FW_SUPABASE_JWT_SECRET set -> HS256, verified locally.
  2. SUPABASE_URL set           -> asymmetric keys fetched from the project JWKS.
  3. neither, and FW_DEV_USER set -> unverified dev principal (local only).

Mode 3 refuses to engage unless FW_ENV is 'dev', so a missing env var in
production fails closed rather than opening the door.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from .db import org_members, orgs


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    org_id: str
    org_name: str
    role: str

    def require(self, minimum: str) -> None:
        if ROLE_RANK.get(self.role, -1) < ROLE_RANK[minimum]:
            raise AuthError(
                f"This action needs the {minimum} role; you have {self.role}.",
                status=403,
            )


_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        base = os.environ["SUPABASE_URL"].rstrip("/")
        # Cached and refreshed by PyJWKClient; key rotation needs no restart.
        _jwks_client = PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def verify_token(token: str) -> dict[str, Any]:
    """Return verified claims, or raise AuthError."""
    secret = os.environ.get("FW_SUPABASE_JWT_SECRET", "").strip()
    options = {"require": ["sub", "exp"]}
    try:
        if secret:
            return jwt.decode(
                token, secret, algorithms=["HS256"],
                audience="authenticated", options=options,
            )
        if os.environ.get("SUPABASE_URL", "").strip():
            key = _jwks().get_signing_key_from_jwt(token).key
            return jwt.decode(
                token, key, algorithms=["ES256", "RS256"],
                audience="authenticated", options=options,
            )
    except jwt.ExpiredSignatureError:
        raise AuthError("Session expired — sign in again.") from None
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid session token: {exc}") from None

    raise AuthError(
        "No auth backend configured. Set FW_SUPABASE_JWT_SECRET or SUPABASE_URL.",
        status=500,
    )


def _dev_principal(engine, org_hint: str | None) -> Principal:
    # Fails closed: FW_ENV must be set to "dev" explicitly. Defaulting to "dev"
    # would mean a host that simply forgot the variable accepts unauthenticated
    # requests as a real member.
    if os.environ.get("FW_ENV", "production") != "dev":
        raise AuthError("Not signed in.", 401)
    email = os.environ.get("FW_DEV_USER", "").strip()
    if not email:
        raise AuthError("Not signed in.", 401)
    # Dev users are still resolved through org_members: the tenancy path is
    # the same one production takes, so it cannot rot untested.
    user_id = os.environ.get("FW_DEV_USER_ID", "00000000-0000-0000-0000-000000000001")
    return _resolve_membership(engine, user_id, email, org_hint)


def _resolve_membership(
    engine, user_id: str, email: str, org_hint: str | None
) -> Principal:
    from sqlalchemy import select

    stmt = (
        select(org_members.c.org_id, org_members.c.role, orgs.c.name)
        .join(orgs, orgs.c.id == org_members.c.org_id)
        .where(org_members.c.user_id == user_id)
        .order_by(org_members.c.created_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()

    if not rows:
        raise AuthError(
            "Your account is not a member of any organisation.", status=403
        )

    chosen = rows[0]
    if org_hint:
        match = next((r for r in rows if str(r.org_id) == org_hint), None)
        if match is None:
            # Do not leak whether the org exists — a non-member and a bad id
            # must be indistinguishable.
            raise AuthError("Organisation not found.", status=404)
        chosen = match

    return Principal(
        user_id=user_id,
        email=email,
        org_id=str(chosen.org_id),
        org_name=chosen.name,
        role=str(chosen.role),
    )


def principal_from_request(engine, authorization: str | None,
                           org_hint: str | None) -> Principal:
    """Resolve the caller. `org_hint` is the X-Org-Id header — a request to
    act for that org, honoured only if membership backs it up."""
    if not authorization:
        return _dev_principal(engine, org_hint)

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Authorization header must be 'Bearer <token>'.")

    claims = verify_token(token)
    if claims.get("exp", 0) < time.time():
        raise AuthError("Session expired — sign in again.")

    email = claims.get("email") or claims.get("user_metadata", {}).get("email", "")
    return _resolve_membership(engine, str(claims["sub"]), email, org_hint)
