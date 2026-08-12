"""Reading a mailbox, and replying into a thread as a draft.

Two things this deliberately does not do.

**It does not send.** The reply is created with `drafts.create`, carrying the
customer's `threadId` and `In-Reply-To`, so it appears in Gmail as a normal
reply sitting in the right conversation — and a person presses send. The scope
requested is `gmail.compose`, which is incapable of sending, so this is a
ceiling Google enforces rather than a promise this module makes.

**It does not subscribe.** There is no `users.watch`, no Pub/Sub topic and no
daily renewal job, because ingestion is a button. That removes the three moving
parts that would each have to work perfectly before a single email arrived, and
it removes the requirement for an always-on host — a sleeping free-tier worker
wakes up when someone presses Sync.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..google import GoogleError, access_token

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# One sync is a foreground request. This caps how much it can pull so the
# response arrives before any reasonable proxy gives up; the UI reports when
# more is waiting and the button can simply be pressed again.
MAX_MESSAGES_PER_SYNC = 40

# How many ids a single sync will page over while looking for unseen ones.
# Listing is cheap (ids only, 100 per call) and this only bites on a mailbox
# where thousands of messages are already stored, so ten calls is a generous
# ceiling that still bounds the request.
MAX_SCANNED_PER_SYNC = 1000

TIMEOUT = 30


def _header(payload: dict[str, Any], name: str) -> str:
    wanted = name.lower()
    for header in (payload.get("headers") or []):
        if str(header.get("name", "")).lower() == wanted:
            return header.get("value") or ""
    return ""


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(h.get("name", "")): (h.get("value") or "")
        for h in (payload.get("headers") or [])
    }


def _decode(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, TypeError):
        return ""


def _walk(part: dict[str, Any], out: dict[str, str]) -> bool:
    """Collect the text and HTML bodies. Returns whether an attachment exists."""
    has_attachment = False
    mime = part.get("mimeType", "")
    filename = part.get("filename") or ""
    body = part.get("body") or {}

    if filename and body.get("attachmentId"):
        has_attachment = True
    elif mime == "text/plain" and not out.get("text"):
        out["text"] = _decode(body.get("data"))
    elif mime == "text/html" and not out.get("html"):
        out["html"] = _decode(body.get("data"))

    for child in (part.get("parts") or []):
        has_attachment = _walk(child, out) or has_attachment
    return has_attachment


class Mailbox:
    """Read access to one connected Gmail account, plus draft creation."""

    def __init__(self, org_id: str, store, apps=None,
                 connection: str = "default", log=print):
        self._org_id = org_id
        self._store = store
        self._apps = apps
        self._connection = connection
        self._log = log
        self._address: str | None = None

    @property
    def address(self) -> str | None:
        return self._address

    def _token(self) -> str:
        token, address = access_token(
            self._org_id, self._store, self._apps, self._connection, self._log
        )
        self._address = address
        return token

    def _auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._token()}

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            resp = requests.request(
                method, f"{API}{path}", headers=self._auth(), timeout=TIMEOUT,
                **kwargs
            )
        except requests.RequestException as exc:
            raise GoogleError(
                f"Could not reach Gmail ({type(exc).__name__})."
            ) from exc
        if resp.status_code == 401:
            raise GoogleError(
                "Gmail rejected the stored credentials — access was probably "
                "revoked. Reconnect this mailbox in Settings."
            )
        if resp.status_code == 403:
            raise GoogleError(
                "Gmail refused the request (403). Check that the mailbox was "
                "connected with read access."
            )
        if resp.status_code not in (200, 201):
            raise GoogleError(f"Gmail returned {resp.status_code}.")
        return resp.json()

    def list_recent(self, lookback_days: int = 7,
                    limit: int = MAX_MESSAGES_PER_SYNC,
                    known: set[str] | None = None) -> tuple[list[str], bool]:
        """Ids we have not already stored, newest first, plus "more remains".

        `known` is what makes repeated syncing work. Gmail returns newest
        first, so an earlier version that simply took the first `limit` ids and
        filtered afterwards returned the *same* newest page every time: once
        those were stored, every later sync fetched them, discarded them all as
        known, and ingested nothing. Anything older than the first page was
        unreachable no matter how many times you pressed the button.

        Skipping known ids *during* pagination instead means each sync walks
        further back through the window, so the button makes progress until
        the whole lookback period has been read.

        Excludes anything we sent and the obvious noise before a single message
        is fetched — the cheapest filter is the one that never downloads a body.
        """
        known = known or set()
        query = (
            f"newer_than:{max(int(lookback_days), 1)}d "
            "-in:sent -in:draft -in:trash -in:spam "
            "-category:promotions -category:social"
        )

        fresh: list[str] = []
        page: str | None = None
        scanned = 0
        while len(fresh) < limit and scanned < MAX_SCANNED_PER_SYNC:
            params: dict[str, Any] = {"q": query, "maxResults": 100}
            if page:
                params["pageToken"] = page
            found = self._call("GET", "/messages", params=params)
            batch = found.get("messages") or []
            scanned += len(batch)
            for message in batch:
                if message["id"] in known:
                    continue
                fresh.append(message["id"])
                if len(fresh) >= limit:
                    break
            page = found.get("nextPageToken")
            if not page:
                # The whole window has been walked; nothing older is waiting.
                return fresh, False

        return fresh, True

    def fetch(self, message_id: str) -> dict[str, Any]:
        """One message, parsed into the shape inbox_emails stores."""
        raw = self._call("GET", f"/messages/{message_id}",
                         params={"format": "full"})
        payload = raw.get("payload") or {}
        bodies: dict[str, str] = {}
        has_attachment = _walk(payload, bodies)

        received = None
        date_header = _header(payload, "Date")
        if date_header:
            try:
                received = parsedate_to_datetime(date_header)
            except (TypeError, ValueError):
                received = None
        if received is None and raw.get("internalDate"):
            from datetime import datetime, timezone

            received = datetime.fromtimestamp(
                int(raw["internalDate"]) / 1000, tz=timezone.utc
            )

        return {
            "gmail_message_id": raw.get("id"),
            "gmail_thread_id": raw.get("threadId"),
            "rfc822_message_id": _header(payload, "Message-ID"),
            "in_reply_to": _header(payload, "In-Reply-To") or None,
            "email_references": _header(payload, "References") or None,
            "from": _header(payload, "From"),
            "subject": _header(payload, "Subject"),
            "body_text": bodies.get("text", ""),
            "body_html": bodies.get("html", ""),
            "snippet": raw.get("snippet") or "",
            "has_attachments": has_attachment,
            "received_at": received,
            "headers": _headers_map(payload),
        }

    def create_reply_draft(self, *, to: str, subject: str, body: str,
                           thread_id: str, in_reply_to: str | None,
                           references: str | None) -> str:
        """A draft reply sitting in the customer's existing conversation.

        `threadId` on the draft plus the `In-Reply-To`/`References` headers are
        both needed: the first is what Gmail threads on, the second is what
        every other mail client threads on.
        """
        address = self._address or self._token() and self._address

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if address:
            message["From"] = address
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = (
                f"{references} {in_reply_to}".strip() if references else in_reply_to
            )
        message.set_content(body)

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        created = self._call("POST", "/drafts", json={
            "message": {"raw": encoded.rstrip("="), "threadId": thread_id},
        })
        return created.get("id", "")

    def profile_address(self) -> str:
        return self._call("GET", "/profile").get("emailAddress", "")
