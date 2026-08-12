"""Xero access for the hosted worker.

Two changes from the desktop `auth.py`, both forced by running server-side:

1. No interactive authorise flow here.  A worker process must never open a
   browser or bind a loopback port -- the consent flow belongs in the web app,
   which stores the resulting refresh token against the org.  This module only
   *refreshes*, which is the only thing a background run legitimately needs.

2. Tokens come from and go back to a `TokenStore`, not %APPDATA%.  The dev
   store is a JSON file (so this spike can reuse the existing desktop token);
   the hosted store is an encrypted per-org row.  Same interface.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Protocol

import requests

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class XeroError(RuntimeError):
    pass


class TokenStore(Protocol):
    def load(self, org_id: str, connection: str) -> dict[str, Any] | None: ...
    def save(self, org_id: str, connection: str, token: dict[str, Any]) -> None: ...


class FileTokenStore:
    """Dev-only store: one JSON file per connection, desktop-app format.

    Lets the spike run against a real Xero org without re-authorising.
    Not for hosted use -- there is no encryption and no tenancy boundary.
    """

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def _path(self, org_id: str, connection: str) -> Path:
        name = "token.json" if connection == "default" else f"token_{connection}.json"
        return self._dir / name

    def load(self, org_id: str, connection: str) -> dict[str, Any] | None:
        p = self._path(org_id, connection)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, org_id: str, connection: str, token: dict[str, Any]) -> None:
        self._path(org_id, connection).write_text(
            json.dumps(token, indent=2), encoding="utf-8"
        )


class XeroCredentials:
    """Implements the CredentialProvider protocol for Xero."""

    def __init__(self, org_id: str, store: TokenStore, log=print, apps=None):
        # `apps` is the per-org application store. Refresh must use the same
        # client the token was issued to, so it resolves through oauth._client
        # exactly as the consent flow did -- using a different client id here
        # would make every refresh fail once an org registers its own app.
        self._org_id = org_id
        self._store = store
        self._log = log
        self._apps = apps

    def xero(self, connection: str = "default") -> tuple[str, str]:
        token = self._store.load(self._org_id, connection)
        if not token or not token.get("refresh_token"):
            raise XeroError(
                f"No Xero connection {connection!r} for this organisation. "
                f"Connect Xero in Settings first."
            )

        obtained = token.get("obtained_at", 0)
        expires_in = token.get("expires_in", 1800)
        if time.time() > obtained + expires_in - 120:
            token = self._refresh(connection, token)

        tenant_id = token.get("tenant_id")
        if not tenant_id:
            tenant_id = self._fetch_tenant(token["access_token"])
            token["tenant_id"] = tenant_id
            self._store.save(self._org_id, connection, token)

        return token["access_token"], tenant_id

    def _refresh(self, connection: str, token: dict) -> dict:
        from .oauth import OAuthError, _client

        try:
            client_id, client_secret, _redirect = _client(self._org_id, self._apps)
        except OAuthError as exc:
            raise XeroError(str(exc)) from None

        self._log(f"[xero] refreshing {connection} token")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
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
            raise XeroError(
                f"Xero token refresh failed ({resp.status_code}): {resp.text}"
            )
        new = resp.json()
        new["obtained_at"] = time.time()
        new["tenant_id"] = token.get("tenant_id")
        new["tenant_name"] = token.get("tenant_name")
        self._store.save(self._org_id, connection, new)
        return new

    @staticmethod
    def _fetch_tenant(access_token: str) -> str:
        resp = requests.get(
            CONNECTIONS_URL,
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise XeroError("Could not list Xero connections: " + resp.text)
        conns = resp.json()
        if not conns:
            raise XeroError("No Xero organisations are connected to this app.")
        return conns[0]["tenantId"]


class XeroClient:
    """Read-only Xero Accounting API client.

    Carried over from the desktop app unchanged apart from the module it
    lives in -- it was already stateless and server-safe.
    """

    def __init__(self, access_token: str, tenant_id: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        }

    def get_bills(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        """All unpaid Accounts Payable bills, paginating until exhausted."""
        statuses = statuses or ["AUTHORISED"]
        bills: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                "Invoices",
                {
                    "Type": "ACCPAY",
                    "Statuses": ",".join(statuses),
                    "page": page,
                    "summaryOnly": "false",
                },
            ).get("Invoices", [])
            bills.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return bills

    def get_receivables(self) -> list[dict[str, Any]]:
        """All unpaid Accounts Receivable invoices — what customers owe.

        Same pagination as bills, opposite direction: ACCREC rather than ACCPAY.
        AUTHORISED means approved and awaiting payment; anything fully paid
        drops out because Xero reports AmountDue of zero, which the caller
        filters.
        """
        invoices: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                "Invoices",
                {
                    "Type": "ACCREC",
                    "Statuses": "AUTHORISED",
                    "page": page,
                    "summaryOnly": "false",
                },
            ).get("Invoices", [])
            invoices.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return invoices

    def get_contacts(self, contact_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Full contact records, keyed by ContactID.

        The Contact embedded in an invoice is a summary: it reliably carries
        the name and id, and frequently omits EmailAddress. Treating that
        omission as "no email address" is wrong -- the address is in Xero, just
        not in that payload. So anything that needs the email must ask the
        Contacts endpoint for it.

        Requested in batches because the ids go in the query string, and a few
        hundred of them would exceed a sane URL length.
        """
        out: dict[str, dict[str, Any]] = {}
        unique = [c for c in dict.fromkeys(contact_ids) if c]
        for i in range(0, len(unique), 50):
            batch = unique[i:i + 50]
            found = self._get("Contacts", {"IDs": ",".join(batch)}).get("Contacts", [])
            for contact in found:
                cid = contact.get("ContactID")
                if cid:
                    out[cid] = contact
        return out

    # -----------------------------------------------------------------------
    # Single-invoice lookups, for answering a customer's email
    # -----------------------------------------------------------------------
    # These carry a short timeout. A pay run can afford to wait a minute for a
    # few hundred bills; someone watching an inbox sync cannot, and the product
    # promises a person sees the email within ten seconds either way.

    LOOKUP_TIMEOUT = 10

    def get_invoices_by_number(self, number: str) -> list[dict[str, Any]]:
        """Every invoice answering to this number.

        Returns the list rather than the first hit on purpose: two matches is a
        meaningful answer (a repeating invoice, or a reused number), and the
        caller must treat it as "unknown" rather than picking one.
        """
        if not (number or "").strip():
            return []
        found = self._get(
            "Invoices",
            {"InvoiceNumbers": number.strip(), "summaryOnly": "false"},
            timeout=self.LOOKUP_TIMEOUT,
        )
        return found.get("Invoices", []) or []

    def find_contacts_by_email(self, email: str) -> list[dict[str, Any]]:
        found = self._get(
            "Contacts",
            {"where": f'EmailAddress=="{email.strip()}"'},
            timeout=self.LOOKUP_TIMEOUT,
        )
        return found.get("Contacts", []) or []

    def find_contacts_by_domain(self, domain: str) -> list[dict[str, Any]]:
        """Contacts whose email is at this domain.

        Xero has no domain operator, so this filters a name-ordered page of
        contacts client-side. Good enough for the secondary lookup, which only
        has to offer a manager something to pick from.
        """
        clean = (domain or "").strip().lower()
        if not clean:
            return []
        found = self._get("Contacts", {"page": 1},
                          timeout=self.LOOKUP_TIMEOUT).get("Contacts", []) or []
        return [
            c for c in found
            if str(c.get("EmailAddress") or "").lower().endswith("@" + clean)
        ]

    def get_open_invoices_for_contact(self, contact_id: str) -> list[dict[str, Any]]:
        found = self._get(
            "Invoices",
            {"ContactIDs": contact_id, "Statuses": "AUTHORISED",
             "summaryOnly": "false"},
            timeout=self.LOOKUP_TIMEOUT,
        )
        return found.get("Invoices", []) or []

    def _get(self, resource: str, params: dict | None = None,
             timeout: int = 60) -> dict[str, Any]:
        url = f"{API_BASE}/{resource}"
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.get(url, headers=self._headers, params=params,
                                    timeout=timeout)
            except requests.Timeout as exc:
                # Named explicitly so the caller can record `timed_out` rather
                # than a generic failure — they are different facts to a person
                # deciding whether to retry or to answer manually.
                raise XeroError(
                    f"Xero timed out after {timeout}s on {resource}."
                ) from exc
            except requests.RequestException as exc:
                raise XeroError(
                    f"Could not reach Xero ({type(exc).__name__}) on {resource}."
                ) from exc
            if resp.ok:
                return resp.json()

            if resp.status_code == 401:
                raise XeroError(
                    "Xero returned 401 Unauthorised — the access token has expired "
                    "or been revoked. Reconnect Xero for this organisation."
                )
            if resp.status_code == 403:
                raise XeroError(
                    "Xero returned 403 Forbidden — check that "
                    "'accounting.invoices.read' is enabled on the Xero app."
                )
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                retry_after = min(int(resp.headers.get("Retry-After", 2**attempt)), 60)
                time.sleep(retry_after)
                continue

            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise XeroError(f"Xero API error {resp.status_code} on {resource}: {detail}")

        raise XeroError(f"Failed to GET {resource} after {_MAX_RETRIES} attempts.")
