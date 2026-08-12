"""Xero consent, server-side.

Which Xero application is used resolves org-first, platform-second. An org can
register its own Xero app and enter the credentials in Settings; an org that
does not rides the platform's, set by environment variable on the worker. That
ordering matters for onboarding: nobody is *forced* through developer.xero.com,
but an org that wants its own consent screen, its own revocation and its own
rate limits can have one -- and one org's credentials are never visible to
another, because every read goes through a store scoped by org_id.

The PKCE verifier is held in `oauth_states` for the length of the round trip,
so it never reaches the browser and cannot be replayed: `take()` consumes the
state exactly once.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse

import requests

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"

# Granular scopes. Xero split the broad `accounting.transactions` scope into
# accounting.invoices / payments / banktransactions / manualjournals, and apps
# created on or after 2 March 2026 have no access to the broad scopes at all --
# requesting one returns invalid_scope before the consent screen appears.
# Scopes for contacts, settings, attachments and budgets were unchanged.
#
# This set is deliberately broad, so that workflows beyond the pay run can be
# added without sending every org back through consent. Note what that means:
# the scopes below WITHOUT a `.read` suffix grant write access to Xero. No code
# in this platform writes to Xero today, and the pay run still cannot move
# money -- it produces a file for the bank. But the permission now exists, so
# "the app is read-only" stopped being true at the token level and the consent
# screen will say so.
#
# Deliberately excluded:
#   accounting.journals.read  -- needs Xero's Advanced tier plus certification;
#                                requesting it without those fails the consent.
#   accounting.reports.*      -- the broad reports scope was replaced by
#                                per-report scopes; add the specific one when a
#                                workflow actually needs a report.
#   payroll / projects / assets / files -- separate APIs, separate scopes; add
#                                when a workflow needs them.
#
# Override per deployment with FW_XERO_SCOPES (space separated).
DEFAULT_SCOPES = (
    "openid profile email offline_access "
    "accounting.contacts accounting.settings accounting.attachments "
    "accounting.invoices accounting.payments "
    "accounting.banktransactions accounting.manualjournals "
    "accounting.budgets.read"
)


class OAuthError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def redirect_uri() -> str:
    return os.environ.get(
        "FW_XERO_REDIRECT_URI", "http://localhost:8000/api/connections/xero/callback"
    ).strip()


def _client(org_id: str | None = None, apps=None) -> tuple[str, str, str]:
    """Resolve the Xero application to use: the org's own, else the platform's.

    The redirect URI is always the platform's, because it points at this
    worker. An org registering its own Xero app still has to register this
    same callback on it -- which is why the UI shows the URI next to the
    credential fields rather than somewhere else.
    """
    client_id = secret = ""
    if apps is not None and org_id:
        found = apps.get(org_id)
        if found:
            client_id, secret = found

    if not client_id:
        client_id = os.environ.get("FW_XERO_CLIENT_ID", "").strip()
        secret = os.environ.get("FW_XERO_CLIENT_SECRET", "").strip()

    if not client_id:
        raise OAuthError(
            "No Xero application is configured. Add your Client ID and secret "
            "in Settings, or set FW_XERO_CLIENT_ID on the worker."
        )
    return client_id, secret, redirect_uri()


def setup_status(org_id: str | None = None, apps=None) -> dict:
    """What the UI needs to talk someone through connecting Xero.

    The redirect URI is reported from the worker's own configuration rather
    than assembled in the browser: it must match what Xero has registered
    byte for byte, so showing a value derived from anywhere else invites a
    mismatch that only appears at the end of the consent flow.
    """
    try:
        client_id, _secret, redirect = _client(org_id, apps)
        configured = True
    except OAuthError:
        client_id, redirect, configured = "", redirect_uri(), False
    app = apps.status(org_id) if (apps is not None and org_id) else {"source": "platform"}
    return {
        "provider": "xero",
        "appConfigured": configured,
        "app": app,
        "redirectUri": redirect,
        "scopes": os.environ.get("FW_XERO_SCOPES", DEFAULT_SCOPES).split(),
        # Never the client secret, and not the client id either: neither is
        # needed to follow the steps, and both are the platform's, not the org's.
        "developerPortal": "https://developer.xero.com/app/manage",
    }


def start(state_store, org_id: str, name: str, user: str, apps=None) -> str:
    """Create a PKCE challenge, stash the verifier, return the consent URL."""
    client_id, _secret, redirect = _client(org_id, apps)

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)

    state_store.put(state, org_id, "xero", name, verifier, user)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": os.environ.get("FW_XERO_SCOPES", DEFAULT_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # quote_via=quote encodes spaces as %20; Xero rejects '+'-separated scopes
    # with invalid_scope.
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )


def exchange(code: str, verifier: str, org_id: str | None = None, apps=None) -> dict:
    # Resolved again rather than carried through the round trip: the org cannot
    # change between start and callback, and re-resolving avoids parking a
    # client secret in the oauth_states row.
    client_id, client_secret, redirect = _client(org_id, apps)
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode("ascii")
        ).decode("ascii")
        headers["Authorization"] = "Basic " + basic
        data.pop("client_id", None)

    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise OAuthError(f"Xero token exchange failed ({resp.status_code}).")

    token = resp.json()
    token["obtained_at"] = time.time()
    return token


def tenants(access_token: str) -> list[dict]:
    resp = requests.get(
        CONNECTIONS_URL,
        headers={"Authorization": "Bearer " + access_token,
                 "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthError("Could not list Xero organisations after consent.")
    return resp.json()
