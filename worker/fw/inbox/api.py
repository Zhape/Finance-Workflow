"""HTTP surface for the Invoice Inbox.

A router rather than more handlers in `server.py`, because this is a second
product surface with its own lifecycle and server.py is already the length it
wants to be. The rules it follows are the platform's, unchanged: every handler
resolves a Principal first, every store call takes `principal.org_id`, and no
handler takes an org id from a request body.

`build_router` takes its collaborators as arguments rather than importing them,
so the engine, the token stores and the thread pool stay singletons owned by
`server.py` and this module stays importable by a test without booting an app.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import Principal
from ..google import GoogleError
from ..xero import XeroClient, XeroError
from . import gemini, render, review, templates as tmpl
from .gmail import Mailbox
from .models import KEY, OUT_OF_SCOPE, State
from .pipeline import Pipeline, SyncReport
from .stores import (
    CategoryStore,
    ClassificationStore,
    DraftStore,
    EmailStore,
    ErrorStore,
    LookupStore,
    MailboxStore,
    ReplyStore,
    SettingsStore,
    TemplateVersionStore,
)

# How many times a Gmail draft creation is retried before it is queued and the
# manager told. Three, then stop: a fourth attempt at a mailbox that has
# refused three times is noise, not resilience.
DRAFT_ATTEMPTS = 3


class CategoryOverride(BaseModel):
    categoryKey: str


class DraftEdit(BaseModel):
    subject: str
    body: str


class SettingsEdit(BaseModel):
    lookbackDays: int
    xeroConnection: str = "default"
    # Null on either means "keep using the default", which is why they are
    # optional rather than defaulted to a guess.
    xeroTenantId: str | None = None
    classifierModel: str | None = None


class NewCategory(BaseModel):
    key: str
    label: str
    description: str


class CategoryEdit(BaseModel):
    label: str | None = None
    description: str | None = None
    enabled: bool | None = None


class TemplateEdit(BaseModel):
    variant: str
    subject: str
    body: str


def build_router(*, engine, principal_dep: Callable[..., Principal],
                 google_connections: Callable[[], Any],
                 google_apps: Callable[[], Any],
                 xero_connections: Callable[[], Any],
                 xero_apps: Callable[[], Any],
                 templates_store, access_store, pool) -> APIRouter:
    """Wire the router. The credential stores arrive as factories, not
    instances, because building one needs FW_ENCRYPTION_KEY — and the platform
    deliberately surfaces a missing key as a 500 on the endpoint that needed it
    rather than as a worker that will not boot."""
    router = APIRouter(prefix="/api/inbox", tags=["inbox"])

    mailboxes = MailboxStore(engine)
    settings = SettingsStore(engine)
    categories = CategoryStore(engine)
    emails = EmailStore(engine)
    classifications = ClassificationStore(engine)
    lookups = LookupStore(engine)
    drafts = DraftStore(engine)
    replies = ReplyStore(engine)
    versions = TemplateVersionStore(engine)
    errors = ErrorStore(engine)

    stores = {
        "categories": categories, "emails": emails,
        "classifications": classifications, "lookups": lookups,
        "drafts": drafts, "replies": replies, "versions": versions,
        "errors": errors,
    }

    # -- helpers ---------------------------------------------------------

    def guard(principal: Principal, minimum: str = "viewer") -> None:
        """Fail closed on access, exactly as the run endpoints do.

        An org without the tool granted gets the same 404 as one asking for a
        tool that does not exist — so the API cannot be used to discover what
        other customers run.
        """
        if not access_store.has(principal.org_id, KEY):
            raise HTTPException(404, "This organisation does not have the "
                                     "Invoice Inbox.")
        if minimum != "viewer":
            principal.require(minimum)

    def template_for(org_id: str, category_key: str) -> dict[str, str]:
        """This org's wording for a category, else the shipped default."""
        override = templates_store.overrides(org_id, KEY).get(category_key)
        if override:
            return {"subject": override["subject"], "body": override["body"]}
        return tmpl.default_for(category_key)

    def merged_templates(org_id: str) -> dict[str, dict[str, Any]]:
        """Every enabled category's template, marked as customised or not."""
        overrides = templates_store.overrides(org_id, KEY)
        out: dict[str, dict[str, Any]] = {}
        for category in categories.list(org_id):
            default = tmpl.default_for(category.key)
            override = overrides.get(category.key)
            out[category.key] = {
                "label": category.label,
                "subject": (override or default)["subject"],
                "body": (override or default)["body"],
                "customised": override is not None,
                "default": default,
                "version": versions.current_version(org_id, category.key),
                "updatedBy": (override or {}).get("updatedBy"),
                "updatedAt": (override or {}).get("updatedAt"),
            }
        return out

    def xero_client(org_id: str) -> tuple[XeroClient | None, str | None, str | None]:
        """(client, tenant id, error) — never raises, so a sync can continue.

        The organisation is the org's explicit choice when it has made one.
        Without that, it is whichever tenant the consent callback happened to
        record — the first one Xero listed — which is how two differently named
        connections ended up pointing at the same company.
        """
        current = settings.get(org_id)
        try:
            from ..xero import XeroCredentials

            store = xero_connections()
            connection = current["xeroConnection"]
            available = [c["name"] for c in store.list(org_id, provider="xero")]
            if connection not in available:
                # The configured name is a default ('default') that an org may
                # simply never have created — its connections might be called
                # 'us' and 'au'. Failing every lookup because of a name is a
                # dead end a person cannot see the way out of, so fall back to
                # a real one and let the tenant picker decide what is read.
                if not available:
                    return None, None, (
                        "No Xero connection for this organisation. Connect "
                        "Xero in Settings first."
                    )
                connection = available[0]

            creds = XeroCredentials(org_id, store, log=lambda m: None,
                                    apps=xero_apps())
            token, tenant_id = creds.xero(connection)
            chosen = current.get("xeroTenantId") or tenant_id
            return XeroClient(token, chosen), chosen, None
        except XeroError as exc:
            return None, None, str(exc)

    def mailbox_client(org_id: str, connection_name: str) -> Mailbox:
        return Mailbox(org_id, google_connections(), google_apps(),
                       connection=connection_name, log=lambda m: None)

    def sender_name(principal: Principal) -> str:
        return principal.email.split("@")[0].replace(".", " ").title()

    def build_pipeline(principal: Principal) -> tuple[Pipeline, str | None]:
        client, tenant_id, xero_error = xero_client(principal.org_id)
        chosen_model = settings.get(principal.org_id).get("classifierModel")
        pipeline = Pipeline(
            org_id=principal.org_id,
            stores=stores,
            classification_client=gemini.client(chosen_model),
            xero=client,
            xero_tenant_id=tenant_id,
            sender_name=sender_name(principal),
            template_source=lambda key: template_for(principal.org_id, key),
            log=lambda m: None,
        )
        return pipeline, xero_error

    def unclassified_ids(org_id: str) -> list[str]:
        """Emails whose label came from the fallback path, not the classifier.

        `source='fallback'` means the classifier never actually answered — it
        was unconfigured, timed out, rate limited, or the circuit was open. The
        email still reached a person with a holding template, which is the
        degraded mode working, but the label is not a judgement and should not
        be treated as one.

        Deliberately narrow: a real classification, a human override, and
        anything already drafted into Gmail are all left alone.
        """
        rows = emails.list(org_id, [State.NEEDS_REVIEW, State.DRAFT_FAILED],
                           limit=1000)
        ids = [r["id"] for r in rows]
        labels = classifications.latest_map(org_id, ids)
        sent = replies.status_map(org_id, ids)
        return [
            email_id for email_id in ids
            if (labels.get(email_id) or {}).get("source") == "fallback"
            and sent.get(email_id) != "created"
        ]

    # -- status and settings ---------------------------------------------

    @router.get("/status")
    def status(principal: Principal = Depends(principal_dep)):
        guard(principal)
        categories.seed(principal.org_id, principal.email)
        connected = xero_connections().list(principal.org_id)
        xero = next((c for c in connected if c["provider"] == "xero"), None)
        return {
            "mailboxes": mailboxes.list(principal.org_id),
            "settings": settings.get(principal.org_id),
            "categories": [c.to_json() for c in categories.list(principal.org_id)],
            "classifier": gemini.status(
                settings.get(principal.org_id).get("classifierModel")),
            "errors": errors.open(principal.org_id),
            "stats": emails.stats(principal.org_id),
            # Drives the re-classify action: how many labels are placeholders
            # left by an outage rather than answers from the classifier.
            "unclassified": len(unclassified_ids(principal.org_id)),
            # Shown permanently, so a wrong organisation is obvious rather than
            # something you discover from a reply quoting the wrong invoice.
            "xero": {
                "connected": xero is not None,
                "tenantName": (xero or {}).get("tenantName"),
                "connection": settings.get(principal.org_id)["xeroConnection"],
            },
        }

    @router.put("/settings")
    def save_settings(body: SettingsEdit,
                      principal: Principal = Depends(principal_dep)):
        guard(principal, "admin")
        if not 1 <= body.lookbackDays <= 30:
            raise HTTPException(
                422, "A sync can look back between 1 and 30 days."
            )
        settings.save(
            principal.org_id,
            lookback_days=body.lookbackDays,
            xero_connection=body.xeroConnection.strip() or "default",
            xero_tenant_id=(body.xeroTenantId or "").strip() or None,
            classifier_model=(body.classifierModel or "").strip() or None,
            user=principal.email,
        )
        return {"ok": True, "settings": settings.get(principal.org_id)}

    @router.get("/xero/organisations")
    def xero_organisations(principal: Principal = Depends(principal_dep)):
        """Every Xero organisation the stored token can actually read.

        A token often reaches several. The consent callback records only the
        first one Xero lists, so the connection's own tenant is a guess — this
        asks Xero directly and lets the org pick, which is the difference
        between a name on a screen and the ledger being read.
        """
        guard(principal)
        current = settings.get(principal.org_id)
        try:
            from .. import oauth
            from ..xero import XeroCredentials

            store = xero_connections()
            available = [c["name"] for c in
                         store.list(principal.org_id, provider="xero")]
            connection = current["xeroConnection"]
            if connection not in available:
                if not available:
                    raise XeroError("No Xero connection for this organisation. "
                                    "Connect Xero in Settings first.")
                connection = available[0]
            creds = XeroCredentials(principal.org_id, store,
                                    log=lambda m: None, apps=xero_apps())
            token, connection_tenant = creds.xero(connection)
            found = oauth.tenants(token)
        except Exception as exc:  # noqa: BLE001 — the message is the answer
            # Not an HTTP error: "Xero is not connected yet" is an ordinary
            # state of a settings screen, not a gateway failure, and raising
            # made it a red line in the browser console instead of a sentence
            # the reader can act on.
            return {"organisations": [], "selected": None,
                    "fromConnection": None, "error": str(exc)}

        return {
            "organisations": [
                {"tenantId": t.get("tenantId"), "name": t.get("tenantName")}
                for t in found
            ],
            "selected": current.get("xeroTenantId") or connection_tenant,
            "fromConnection": connection_tenant,
            "error": None,
        }

    @router.get("/classifier/models")
    def classifier_models(principal: Principal = Depends(principal_dep)):
        """Model names this API key can actually use.

        Offered because the alternative is typing a name into a hosting
        dashboard, redeploying, and reading the resulting 404s — which is
        exactly how this endpoint came to exist.
        """
        guard(principal, "admin")
        chosen = settings.get(principal.org_id).get("classifierModel")
        try:
            names = gemini.available_models()
        except Exception as exc:  # noqa: BLE001 — the message is the answer
            return {"configured": gemini.configured(), "models": [],
                    "selected": chosen, "error": str(exc)}
        return {"configured": True, "models": names, "selected": chosen,
                "inUse": chosen or gemini.model_name(), "error": None}

    @router.post("/classifier/reset")
    def reset_classifier(principal: Principal = Depends(principal_dep)):
        """Close the circuit after fixing the cause, without waiting it out."""
        guard(principal, "member")
        gemini.CIRCUIT.record_success()
        return {"ok": True, "classifier": gemini.status(
            settings.get(principal.org_id).get("classifierModel"))}

    @router.post("/reclassify")
    def reclassify(principal: Principal = Depends(principal_dep)):
        """Queue everything an outage failed to classify for another attempt.

        Without this a classifier outage is permanent. A triaged email never
        returns to the pending set, so pressing Sync again does nothing at all
        — the mail stays labelled by the fallback for ever, and the only cure
        was someone editing the database.
        """
        guard(principal, "member")
        pending = unclassified_ids(principal.org_id)
        if not pending:
            return {"ok": True, "reset": 0,
                    "message": "Every email has a real classification."}

        # Give the classifier a fresh chance: a cooling circuit would otherwise
        # refuse the first calls of the very sync this is preparing for.
        gemini.CIRCUIT.record_success()
        reset = emails.reset_for_retriage(principal.org_id, pending)
        errors.resolve_all(principal.org_id)
        return {"ok": True, "reset": reset,
                "message": f"{reset} email(s) queued. Press Sync to classify "
                           f"them — 25 at a time."}

    # -- mailboxes -------------------------------------------------------

    @router.post("/mailboxes/start")
    def connect_mailbox(principal: Principal = Depends(principal_dep)):
        """Begin consent for a mailbox the inbox will read.

        A separate flow from the chase-up's Gmail connection, and a separate
        connection row: that one is draft-only by scope and stays that way.
        """
        guard(principal, "admin")
        from .. import google

        try:
            url = google.start(
                _states, principal.org_id, "inbox", principal.email,
                google_apps(), scopes=google.INBOX_SCOPES,
            )
        except GoogleError as exc:
            raise HTTPException(500, str(exc)) from None
        return {"url": url}

    @router.delete("/mailboxes/{mailbox_id}")
    def remove_mailbox(mailbox_id: str,
                       principal: Principal = Depends(principal_dep)):
        guard(principal, "admin")
        connection = mailboxes.remove(principal.org_id, mailbox_id)
        if connection is None:
            raise HTTPException(404, "No such mailbox.")
        google_connections().disconnect(principal.org_id, connection)
        return {"ok": True}

    # -- sync ------------------------------------------------------------

    @router.post("/sync")
    def sync(principal: Principal = Depends(principal_dep)):
        """Pull, triage and draft. The whole ingestion path, on a button.

        Blocking on purpose, like a run: the person pressing it should leave
        knowing what arrived, not wondering. It is bounded so it cannot outlive
        a request, and it reports when more is still waiting.
        """
        guard(principal, "member")
        categories.seed(principal.org_id, principal.email)

        connected = mailboxes.list(principal.org_id)
        if not connected:
            raise HTTPException(
                409, "No mailbox is connected. Connect one in Settings first."
            )

        report = SyncReport()
        pipeline, xero_error = build_pipeline(principal)
        if xero_error:
            errors.add(principal.org_id, "verify", "XRO-002", xero_error)

        own = mailboxes.addresses(principal.org_id)
        lookback = settings.get(principal.org_id)["lookbackDays"]

        def work() -> SyncReport:
            for mailbox in connected:
                try:
                    client = mailbox_client(principal.org_id,
                                            mailbox["connectionName"])
                    pipeline.ingest_mailbox(mailbox, client, lookback, own,
                                            report)
                    mailboxes.record_sync(principal.org_id, mailbox["id"])
                except GoogleError as exc:
                    report.mailbox_errors.append(f"{mailbox['address']}: {exc}")
                    mailboxes.record_sync(principal.org_id, mailbox["id"],
                                          error=str(exc)[:300])
                    errors.add(principal.org_id, "ingest", "ING-001", str(exc))
            pipeline.triage_pending(report)
            return report

        result = pool.submit(work).result()
        return {"ok": True, "report": result.to_json(),
                "stats": emails.stats(principal.org_id),
                "errors": errors.open(principal.org_id)}

    # -- the list --------------------------------------------------------

    @router.get("/emails")
    def list_emails(state: str | None = None,
                    principal: Principal = Depends(principal_dep)):
        """The left pane: one card per thread, oldest waiting first.

        Threads are collapsed to their most recent message — a customer who has
        written four times is one thing to deal with, not four — and the whole
        thread is available behind the card.
        """
        guard(principal)
        wanted = [state] if state else [
            State.NEEDS_REVIEW, State.DRAFTED, State.DRAFT_FAILED
        ]
        rows = emails.list(principal.org_id, wanted)

        latest_per_thread: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for row in rows:
            thread = row["threadId"] or row["id"]
            counts[thread] = counts.get(thread, 0) + 1
            # `rows` is newest first, so the first seen wins.
            latest_per_thread.setdefault(thread, row)

        cards = list(latest_per_thread.values())
        ids = [c["id"] for c in cards]
        labels = classifications.latest_map(principal.org_id, ids)
        reply_status = replies.status_map(principal.org_id, ids)

        for card in cards:
            label = labels.get(card["id"]) or {}
            card["category"] = label.get("categoryKey")
            card["confidence"] = label.get("confidence")
            card["multiIntent"] = label.get("multiIntent", False)
            card["invoiceNumber"] = (label.get("extracted") or {}).get(
                "invoiceNumber")
            card["threadCount"] = counts.get(card["threadId"] or card["id"], 1)
            card["replyStatus"] = reply_status.get(card["id"])
            # Bodies are heavy and the list does not show them.
            card.pop("bodyText", None)
            card.pop("bodyStripped", None)

        # Oldest first: the order a person actually works a queue.
        cards.sort(key=lambda c: c["receivedAt"] or "")
        return {"emails": cards, "stats": emails.stats(principal.org_id)}

    # -- the review pane -------------------------------------------------

    @router.get("/emails/{email_id}")
    def get_email(email_id: str, principal: Principal = Depends(principal_dep)):
        guard(principal)
        email = emails.get(principal.org_id, email_id)
        if email is None:
            raise HTTPException(404, "No such email.")

        classification = classifications.latest(principal.org_id, email_id)
        found = lookups.latest(principal.org_id, email_id)
        draft = drafts.latest(principal.org_id, email_id)
        reply = replies.get(principal.org_id, email_id)

        flags = review.flags(
            classification, found,
            is_reply_to_us=bool(email.get("inReplyTo")),
            has_open_dispute=emails.has_open_dispute(
                principal.org_id, email["fromEmail"]),
            category_key=(draft or {}).get("categoryKey"),
        )
        blocking = review.blockers(
            (draft or {}).get("subject", ""), (draft or {}).get("body", ""),
            email.get("fromEmail"),
        ) if draft else [{"code": "no_draft", "message": "No draft yet."}]

        return {
            "email": email,
            "thread": emails.thread(principal.org_id, email["threadId"]),
            "senderHistory": emails.from_sender(
                principal.org_id, email["fromEmail"], email_id),
            "classification": classifications.history(
                principal.org_id, email_id),
            "extracted": (classification.extracted.to_json()
                          if classification else None),
            "lookup": found,
            "draft": draft,
            "reply": reply,
            "flags": flags,
            "blockers": blocking,
            "categories": [c.to_json()
                           for c in categories.enabled(principal.org_id)],
            # Shipped so a category change re-renders in the browser rather
            # than after a round trip.
            "templates": merged_templates(principal.org_id),
        }

    @router.put("/emails/{email_id}/category")
    def override_category(email_id: str, body: CategoryOverride,
                          principal: Principal = Depends(principal_dep)):
        """Correct the category and re-render.

        The AI's original suggestion is not edited — a second classification row
        is written with source='human', so both survive in the trail.
        """
        guard(principal, "member")
        email = emails.get(principal.org_id, email_id)
        if email is None:
            raise HTTPException(404, "No such email.")
        if replies.get(principal.org_id, email_id) is not None and \
                (replies.get(principal.org_id, email_id) or {}).get(
                    "status") == "created":
            raise HTTPException(
                409, "A draft reply already exists for this email."
            )

        enabled = {c.key for c in categories.enabled(principal.org_id)}
        if body.categoryKey not in enabled:
            raise HTTPException(422, f"Unknown category {body.categoryKey!r}.")

        previous = classifications.latest(principal.org_id, email_id)
        if previous is not None:
            corrected = previous
            corrected.category = body.categoryKey
            corrected.source = "human"
            corrected.confidence = 1.0
            classifications.add(principal.org_id, email_id, corrected,
                                user=principal.email)

        found = lookups.latest(principal.org_id, email_id)
        pipeline, _ = build_pipeline(principal)
        pipeline.draft(email, body.categoryKey, found, previous,
                       user=principal.email)
        return get_email(email_id, principal)

    @router.put("/emails/{email_id}/draft")
    def edit_draft(email_id: str, body: DraftEdit,
                   principal: Principal = Depends(principal_dep)):
        """Save a hand-edited draft. Free text is allowed and expected."""
        guard(principal, "member")
        email = emails.get(principal.org_id, email_id)
        if email is None:
            raise HTTPException(404, "No such email.")
        current = drafts.latest(principal.org_id, email_id)
        if current is None:
            raise HTTPException(409, "There is no draft to edit yet.")

        blocking = review.blockers(body.subject, body.body,
                                   email.get("fromEmail"))
        drafts.add(
            principal.org_id, email_id,
            category_key=current["categoryKey"],
            template_version=current.get("templateVersion"),
            subject=body.subject, body=body.body,
            fields=current.get("fields") or {},
            missing=render.unfilled(body.body) + render.unfilled(body.subject),
            blockers=blocking, edited=True, user=principal.email,
        )
        return {"ok": True, "blockers": blocking}

    # -- creating the Gmail draft ----------------------------------------

    @router.post("/emails/{email_id}/draft-reply")
    def create_reply(email_id: str,
                     principal: Principal = Depends(principal_dep)):
        """Put the reply in Gmail, as a draft in the customer's own thread.

        Nothing is sent. The person who owns the relationship opens Gmail,
        reads it, and presses send.
        """
        guard(principal, "member")
        email = emails.get(principal.org_id, email_id)
        if email is None:
            raise HTTPException(404, "No such email.")

        draft = drafts.latest(principal.org_id, email_id)
        if draft is None:
            raise HTTPException(409, "There is no draft for this email yet.")

        blocking = review.blockers(draft["subject"], draft["body"],
                                   email.get("fromEmail"))
        if blocking:
            raise HTTPException(422, detail={"blockers": blocking})

        # Reserve first. The unique constraint on email_id is what makes a
        # double click, a second tab and a replayed request all resolve to one
        # draft — not the button's disabled state.
        reply_id = replies.reserve(
            principal.org_id, email_id,
            category_key=draft["categoryKey"],
            template_version=draft.get("templateVersion"),
            subject=draft["subject"], body=draft["body"],
            fields=draft.get("fields") or {}, actor=principal.email,
        )
        if reply_id is None:
            existing = replies.get(principal.org_id, email_id) or {}
            if existing.get("status") == "created":
                # Not an error. The other tab already did it.
                return {"ok": True, "alreadyDrafted": True, "reply": existing}
            reply_id = replies.claim_failed(
                principal.org_id, email_id, subject=draft["subject"],
                body=draft["body"], fields=draft.get("fields") or {},
                category_key=draft["categoryKey"], actor=principal.email,
            )
            if reply_id is None:
                raise HTTPException(409, "A reply is already being created.")

        mailbox = next(
            (m for m in mailboxes.list(principal.org_id)
             if m["id"] == email["mailboxId"]), None
        )
        if mailbox is None:
            replies.fail(principal.org_id, reply_id,
                         "The mailbox this email arrived in is gone.", 0)
            raise HTTPException(409, "That mailbox is no longer connected.")

        client = mailbox_client(principal.org_id, mailbox["connectionName"])
        last_error = ""
        for attempt in range(1, DRAFT_ATTEMPTS + 1):
            try:
                draft_id = client.create_reply_draft(
                    to=email["fromEmail"],
                    subject=draft["subject"],
                    body=draft["body"],
                    thread_id=email["threadId"],
                    in_reply_to=email.get("rfc822MessageId"),
                    references=email.get("references"),
                )
            except GoogleError as exc:
                last_error = str(exc)
                if attempt < DRAFT_ATTEMPTS:
                    time.sleep(2 ** attempt)
                continue

            replies.complete(principal.org_id, reply_id, draft_id=draft_id,
                             thread_id=email["threadId"],
                             mailbox_address=client.address, attempts=attempt)
            emails.set_state(principal.org_id, email_id, State.DRAFTED)
            return {"ok": True, "reply": replies.get(principal.org_id, email_id)}

        # Three failures. Queue it, tell someone, and never show "drafted".
        replies.fail(principal.org_id, reply_id, last_error, DRAFT_ATTEMPTS)
        emails.set_state(principal.org_id, email_id, State.DRAFT_FAILED,
                         last_error[:300])
        errors.add(principal.org_id, "draft", "DRF-001", last_error,
                   email_id=email_id, attempts=DRAFT_ATTEMPTS)
        raise HTTPException(
            502,
            f"Gmail refused to create the draft after {DRAFT_ATTEMPTS} "
            f"attempts: {last_error}",
        )

    @router.post("/emails/{email_id}/dismiss")
    def dismiss(email_id: str, principal: Principal = Depends(principal_dep)):
        guard(principal, "member")
        if emails.get(principal.org_id, email_id) is None:
            raise HTTPException(404, "No such email.")
        emails.set_state(principal.org_id, email_id, State.DISMISSED,
                         f"Closed by {principal.email}")
        return {"ok": True}

    # -- categories ------------------------------------------------------

    @router.post("/categories")
    def add_category(body: NewCategory,
                     principal: Principal = Depends(principal_dep)):
        """Add a bucket. The classifier learns it from `description`.

        No deploy: the response schema is rebuilt from the enabled rows on the
        next classification call.
        """
        guard(principal, "admin")
        key = body.key.strip()
        if not key.isidentifier():
            raise HTTPException(
                422, "A category key must be a single word, letters and "
                     "underscores only."
            )
        if key in {c.key for c in categories.list(principal.org_id)}:
            raise HTTPException(409, f"A category called {key!r} already exists.")
        if len(body.description.strip()) < 20:
            raise HTTPException(
                422, "Describe the category in a sentence — this text is what "
                     "the classifier reads to recognise it."
            )
        categories.create(principal.org_id, key, body.label.strip(),
                          body.description.strip(), principal.email)
        return {"ok": True,
                "categories": [c.to_json()
                               for c in categories.list(principal.org_id)]}

    @router.put("/categories/{key}")
    def edit_category(key: str, body: CategoryEdit,
                      principal: Principal = Depends(principal_dep)):
        guard(principal, "admin")
        if key == OUT_OF_SCOPE and body.enabled is False:
            raise HTTPException(
                409, "Out of scope is where anything unrecognised lands. It "
                     "cannot be turned off."
            )
        values = {k: v for k, v in {
            "label": body.label, "description": body.description,
            "enabled": body.enabled,
        }.items() if v is not None}
        if not values:
            raise HTTPException(422, "Nothing to change.")
        if not categories.update(principal.org_id, key, **values):
            raise HTTPException(404, "No such category.")
        return {"ok": True,
                "categories": [c.to_json()
                               for c in categories.list(principal.org_id)]}

    # -- templates -------------------------------------------------------

    @router.get("/templates")
    def get_templates(principal: Principal = Depends(principal_dep)):
        guard(principal)
        categories.seed(principal.org_id, principal.email)
        return {
            "placeholders": [{"token": token, "description": description}
                             for token, description in tmpl.PLACEHOLDERS],
            "templates": merged_templates(principal.org_id),
        }

    @router.put("/templates")
    def save_template(body: TemplateEdit,
                      principal: Principal = Depends(principal_dep)):
        guard(principal, "member")
        known = {c.key for c in categories.list(principal.org_id)}
        if body.variant not in known:
            raise HTTPException(422, f"Unknown category {body.variant!r}.")
        if not body.body.strip():
            raise HTTPException(422, "The message body cannot be empty.")

        unknown = [
            name for name in
            set(render.slots(body.body)) | set(render.slots(body.subject))
            if name not in tmpl.PLACEHOLDER_NAMES
        ]
        if unknown:
            listed = ", ".join("{{" + n + "}}" for n in sorted(unknown))
            raise HTTPException(
                422,
                f"{listed} is not a placeholder this product can fill. A slot "
                f"nothing fills would reach a customer as literal text."
            )

        templates_store.save(principal.org_id, KEY, body.variant, body.subject,
                             body.body, principal.email)
        version = versions.add(principal.org_id, body.variant, body.subject,
                               body.body, principal.email)
        return {"ok": True, "version": version,
                "templates": merged_templates(principal.org_id)}

    @router.delete("/templates/{variant}")
    def reset_template(variant: str,
                       principal: Principal = Depends(principal_dep)):
        """Revert to the product default, in one action."""
        guard(principal, "member")
        if not templates_store.reset(principal.org_id, KEY, variant):
            raise HTTPException(404, "That template is not customised.")
        return {"ok": True, "templates": merged_templates(principal.org_id)}

    # -- errors ----------------------------------------------------------

    @router.post("/errors/dismiss")
    def dismiss_errors(principal: Principal = Depends(principal_dep)):
        guard(principal, "member")
        return {"ok": True, "cleared": errors.resolve_all(principal.org_id)}

    return router


# `server.py` injects the shared OAuth state store here at wiring time. Kept
# module-level rather than threaded through build_router because only the
# consent kickoff needs it, and a router argument used by one handler out of
# twenty is noise at every other call site.
_states = None


def set_state_store(store) -> None:
    global _states
    _states = store
