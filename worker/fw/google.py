"""Gmail: consent, refresh, and drafting.

**Drafts, not sends.** The scope requested is `gmail.compose`, which can create
a draft in someone's mailbox and cannot send it. That is a deliberate ceiling,
not an oversight:

  * A chase-up is a relationship. The person whose name is on the email should
    read it before a customer does.
  * A bug in a loop that sends is unrecoverable; a bug in a loop that drafts is
    a tedious afternoon deleting drafts.
  * `gmail.send` is a restricted scope requiring Google's security assessment.
    `gmail.compose` is too, in production, but the review is easier to pass
    when the app demonstrably cannot send.

The draft lands in the connected mailbox, ready to review and press send.

Structurally this mirrors `oauth.py` for Xero: the same `connections` and
`provider_apps` tables, keyed by provider, so an org's Google credentials are
stored, scoped and encrypted exactly as its Xero ones are.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from email.message import EmailMessage

import requests

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"

# gmail.compose creates drafts. It cannot send. openid/email identify which
# mailbox was connected, so the UI can show it.
DEFAULT_SCOPES = (
    "openid email https://www.googleapis.com/auth/gmail.compose"
)

PROVIDER = "google"


class GoogleError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def redirect_uri() -> str:
    return os.environ.get(
        "FW_GOOGLE_REDIRECT_URI",
        "http://localhost:8000/api/connections/google/callback",
    ).strip()


def _client(org_id: str | None = None, apps=None) -> tuple[str, str, str]:
    """The Google application: the org's own, else the platform's."""
    client_id = secret = ""
    if apps is not None and org_id:
        found = apps.get(org_id)
        if found:
            client_id, secret = found
    if not client_id:
        client_id = os.environ.get("FW_GOOGLE_CLIENT_ID", "").strip()
        secret = os.environ.get("FW_GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id:
        raise GoogleError(
            "No Google application is configured. Add a Client ID and secret "
            "in Settings, or set FW_GOOGLE_CLIENT_ID on the worker."
        )
    return client_id, secret, redirect_uri()


def setup_status(org_id: str | None = None, apps=None) -> dict:
    try:
        _client(org_id, apps)
        configured = True
    except GoogleError:
        configured = False
    app = apps.status(org_id) if (apps is not None and org_id) else {"source": "platform"}
    return {
        "provider": PROVIDER,
        "appConfigured": configured,
        "app": app,
        "redirectUri": redirect_uri(),
        "scopes": os.environ.get("FW_GOOGLE_SCOPES", DEFAULT_SCOPES).split(),
        "developerPortal": "https://console.cloud.google.com/apis/credentials",
        "canSend": False,
    }


def start(state_store, org_id: str, name: str, user: str, apps=None) -> str:
    client_id, _secret, redirect = _client(org_id, apps)

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)
    state_store.put(state, org_id, PROVIDER, name, verifier, user)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": os.environ.get("FW_GOOGLE_SCOPES", DEFAULT_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Google only returns a refresh token when it is asked to, and only the
        # first time unless consent is forced. A background run that cannot
        # refresh is useless, so both are non-negotiable here.
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )


def exchange(code: str, verifier: str, org_id: str | None = None, apps=None) -> dict:
    client_id, client_secret, redirect = _client(org_id, apps)
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        raise GoogleError(f"Google token exchange failed ({resp.status_code}).")
    token = resp.json()
    token["obtained_at"] = time.time()
    return token


def mailbox(access_token: str) -> str:
    """Which mailbox was connected — shown in Settings so it is unambiguous."""
    resp = requests.get(
        PROFILE_URL,
        headers={"Authorization": "Bearer " + access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise GoogleError("Could not read the Gmail profile after consent.")
    return resp.json().get("emailAddress", "")


class GmailDrafter:
    """Creates drafts in a connected mailbox. Has no way to send one."""

    def __init__(self, org_id: str, store, apps=None, connection: str = "default",
                 log=print):
        self._org_id = org_id
        self._store = store
        self._apps = apps
        self._connection = connection
        self._log = log
        self._address: str | None = None

    @property
    def address(self) -> str | None:
        return self._address

    def _access_token(self) -> str:
        token = self._store.load(self._org_id, self._connection)
        if not token or not token.get("refresh_token"):
            raise GoogleError(
                "Gmail is not connected for this organisation. Connect it in "
                "Settings."
            )
        self._address = token.get("mailbox")
        obtained = token.get("obtained_at", 0)
        expires_in = token.get("expires_in", 3600)
        if time.time() > obtained + expires_in - 120:
            token = self._refresh(token)
        return token["access_token"]

    def _refresh(self, token: dict) -> dict:
        client_id, client_secret, _redirect = _client(self._org_id, self._apps)
        self._log("[gmail] refreshing token")
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=30)
        if resp.status_code != 200:
            raise GoogleError(
                f"Gmail token refresh failed ({resp.status_code}). Reconnect "
                f"Gmail in Settings."
            )
        new = resp.json()
        new["obtained_at"] = time.time()
        # Google does not return the refresh token again on refresh.
        new.setdefault("refresh_token", token["refresh_token"])
        new["mailbox"] = token.get("mailbox")
        self._store.save(self._org_id, self._connection, new)
        return new

    def create_draft(self, to: str, subject: str, body: str) -> str:
        """Create one draft. Returns its id. Never sends."""
        access = self._access_token()

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if self._address:
            message["From"] = self._address
        message.set_content(body)

        resp = requests.post(
            DRAFTS_URL,
            headers={"Authorization": "Bearer " + access,
                     "Content-Type": "application/json"},
            json={"message": {"raw": _b64url(message.as_bytes())}},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise GoogleError(
                f"Could not create the Gmail draft ({resp.status_code})."
            )
        return resp.json().get("id", "")
