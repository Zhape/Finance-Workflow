"""Worker API.

Every handler resolves a Principal first and passes `principal.org_id` into
the stores. No handler ever takes an org id from the request body -- the only
client-supplied hint is the X-Org-Id header, and `auth.py` honours it only
when membership backs it up.

Runs execute in a thread pool. A Xero pull of a few hundred bills already
exceeds a serverless timeout, which is why this service cannot live on Vercel
alongside the web app.
"""

from __future__ import annotations

import os
import traceback
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from . import env, google, oauth, requests_pr, workflows

env.load()  # before anything reads os.environ

from .auth import AuthError, Principal, principal_from_request
from .contract import ParamError, RunContext, RunStatus
from .crypto import Cipher, EncryptionError
from .db import create_db_engine, init_db
from .stores import (
    ConnectionStore,
    ProviderAppStore,
    LayoutStore,
    OAuthStateStore,
    OrgStore,
    RunStore,
    TemplateStore,
    VendorStore,
    WorkflowAccessStore,
    WorkflowRequestStore,
)
from .xero import XeroCredentials, XeroError

app = FastAPI(title="finance-workflows worker")

WEB_ORIGIN = os.environ.get("FW_WEB_ORIGIN", "http://localhost:5174")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ENGINE = create_db_engine()
init_db(ENGINE)

POOL = ThreadPoolExecutor(max_workers=4)
RUNS = RunStore(ENGINE)
ORGS = OrgStore(ENGINE)
STATES = OAuthStateStore(ENGINE)
LAYOUTS = LayoutStore(ENGINE)
ACCESS = WorkflowAccessStore(ENGINE)
TEMPLATES = TemplateStore(ENGINE)
REQUESTS = WorkflowRequestStore(ENGINE)


# Providers whose OAuth application an org may bring its own of. Keyed by the
# same string used in `connections.provider` and `provider_apps.provider`.
PROVIDERS = ("xero", "google")


def _apps_for(provider: str) -> ProviderAppStore:
    if provider not in PROVIDERS:
        raise HTTPException(404, f"Unknown provider {provider!r}")
    return ProviderAppStore(ENGINE, Cipher(), provider=provider)


def _connections_for(provider: str) -> ConnectionStore:
    return ConnectionStore(ENGINE, Cipher(), provider=provider)


def _apps() -> ProviderAppStore:
    return ProviderAppStore(ENGINE, Cipher())


def _google_apps() -> ProviderAppStore:
    return _apps_for(google.PROVIDER)


def _google_connections() -> ConnectionStore:
    return _connections_for(google.PROVIDER)


def _mailer(org_id: str):
    """A drafter if this org has connected Gmail, else None.

    None rather than an exception: a workflow must degrade to producing a file
    rather than failing because email is not set up."""
    if _google_connections().load(org_id, "default") is None:
        return None
    return google.GmailDrafter(org_id, _google_connections(), _google_apps(),
                               log=lambda m: None)


def _action_for(workflow_key: str, org_id: str) -> dict[str, str]:
    """The approve button's wording for this workflow, in this org's state."""
    try:
        module = workflows.get(workflow_key)
    except KeyError:
        return {"label": "Approve", "hint": ""}
    decide = getattr(module, "approve_action", None)
    if decide:
        return decide(_mailer(org_id) is not None)
    return {"label": module.SPEC.approve_label, "hint": ""}


def _run_json(run: dict | None, org_id: str) -> dict | None:
    if run is None:
        return None
    run["action"] = _action_for(run["workflow"], org_id)
    return run


def _defaults_for(module):
    """The wording a workflow ships with, if it has any."""
    getter = getattr(module, "default_templates", None)
    return getter() if getter else {}


def _connections() -> ConnectionStore:
    # Built per request so a missing/incorrect FW_ENCRYPTION_KEY surfaces as a
    # clear 500 on the endpoint that needs it, not as a boot crash.
    return ConnectionStore(ENGINE, Cipher())


def me(
    authorization: str | None = Header(default=None),
    x_org_id: str | None = Header(default=None),
) -> Principal:
    try:
        return principal_from_request(ENGINE, authorization, x_org_id)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@app.exception_handler(AuthError)
async def _auth_error(_request: Request, exc: AuthError):
    return Response(content=str(exc), status_code=exc.status)


class StartRun(BaseModel):
    workflow: str
    params: dict = {}


class Approve(BaseModel):
    rowIds: list[str]


class ImportVendors(BaseModel):
    region: str
    records: list[dict]
    columns: list[str] | None = None


# ---------------------------------------------------------------------------
# Session and catalogue
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness for the platform's health check. No auth, no data — just proof
    the process is up and the database is reachable."""
    from sqlalchemy import text

    try:
        with ENGINE.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        # Log it as well as returning it. A platform health check only records
        # the status code, so a body-only reason leaves "503" in the logs with
        # no way to tell a wrong password from an unreachable host.
        detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        print(f"[health] database unreachable: {type(exc).__name__}: {detail}",
              flush=True)
        raise HTTPException(503, f"database unreachable: {type(exc).__name__}")
    return {"status": "ok"}


@app.get("/api/me")
def whoami(principal: Principal = Depends(me)):
    return {
        "email": principal.email,
        "userId": principal.user_id,
        "org": {"id": principal.org_id, "name": principal.org_name},
        "role": principal.role,
    }


@app.get("/api/workflows")
def list_workflows(principal: Principal = Depends(me)):
    """Only the workflows this org has.

    Filtered rather than flagged: a workflow an org does not have should not
    appear at all, because a greyed-out tile still tells them what other
    customers run.
    """
    allowed = ACCESS.keys(principal.org_id)
    out = []
    for spec in workflows.specs():
        if spec.key not in allowed:
            continue
        item = spec.to_json()
        # Whether the workflow has editable wording is a property of the
        # module, not something the UI should hardcode by key.
        item["hasTemplates"] = bool(_defaults_for(workflows.get(spec.key)))
        out.append(item)
    return {"workflows": out}


class WorkflowGrant(BaseModel):
    workflow: str


@app.put("/api/workflows/access")
def grant_workflow(body: WorkflowGrant, principal: Principal = Depends(me)):
    principal.require("admin")
    try:
        workflows.get(body.workflow)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    ACCESS.grant(principal.org_id, body.workflow, principal.email)
    return {"ok": True, "workflows": sorted(ACCESS.keys(principal.org_id))}


@app.delete("/api/workflows/access/{workflow}")
def revoke_workflow(workflow: str, principal: Principal = Depends(me)):
    principal.require("admin")
    if not ACCESS.revoke(principal.org_id, workflow):
        raise HTTPException(404, "This organisation does not have that workflow.")
    return {"ok": True, "workflows": sorted(ACCESS.keys(principal.org_id))}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@app.get("/api/connections")
def list_connections(principal: Principal = Depends(me)):
    return {"connections": _connections().list(principal.org_id)}


@app.get("/api/connections/xero/setup")
def xero_setup(principal: Principal = Depends(me)):
    """Configuration the Settings page needs to walk someone through connecting."""
    return oauth.setup_status(principal.org_id, _apps())


@app.post("/api/connections/xero/start")
def start_xero(name: str = "default", principal: Principal = Depends(me)):
    principal.require("admin")
    try:
        url = oauth.start(STATES, principal.org_id, name, principal.email, _apps())
    except oauth.OAuthError as exc:
        raise HTTPException(500, str(exc)) from None
    return {"url": url}


@app.get("/api/connections/xero/callback")
def xero_callback(code: str | None = None, state: str | None = None,
                  error: str | None = None):
    """Xero redirects the browser here. No Principal: the caller is Xero, and
    the state token is what ties the redirect back to an org."""
    if error:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error={error}")
    if not code or not state:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=missing_code")

    pending = STATES.take(state)
    if pending is None:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=expired_state")

    try:
        token = oauth.exchange(code, pending["verifier"], pending["org_id"], _apps())
        found = oauth.tenants(token["access_token"])
        if not found:
            return RedirectResponse(f"{WEB_ORIGIN}/settings?error=no_organisations")
        token["tenant_id"] = found[0]["tenantId"]
        token["tenant_name"] = found[0].get("tenantName", "")
        token["connected_by"] = pending["created_by"]
        _connections().save(pending["org_id"], pending["name"], token)
    except (oauth.OAuthError, EncryptionError):
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=exchange_failed")

    return RedirectResponse(f"{WEB_ORIGIN}/settings?connected={pending['name']}")


class ProviderApp(BaseModel):
    clientId: str
    clientSecret: str
    label: str | None = None


XeroApp = ProviderApp  # the model is provider-agnostic; name kept for clarity


def _drop_connections(provider: str, org_id: str) -> list[str]:
    """Forget an org's tokens for a provider.

    Called whenever the application changes. A refresh token is bound to the
    client that issued it, so a token kept across an app change fails with
    `invalid_grant` at the least convenient moment -- mid run, rather than at
    the moment someone deliberately changed the setting.
    """
    store = _connections_for(provider)
    dropped = [c["name"] for c in store.list(org_id)]
    for name in dropped:
        store.disconnect(org_id, name)
    return dropped


@app.put("/api/connections/{provider}/app")
def save_provider_app(provider: str, body: XeroApp,
                      principal: Principal = Depends(me)):
    """Register this org's own OAuth application for a provider."""
    principal.require("admin")
    apps = _apps_for(provider)
    if not body.clientId.strip() or not body.clientSecret.strip():
        raise HTTPException(422, "Both the Client ID and the secret are required.")
    apps.save(principal.org_id, body.clientId, body.clientSecret,
              principal.email, body.label)
    return {"ok": True, "disconnected": _drop_connections(provider, principal.org_id)}


@app.delete("/api/connections/{provider}/app")
def clear_provider_app(provider: str, principal: Principal = Depends(me)):
    """Revert this org to the platform's application for a provider."""
    principal.require("admin")
    if not _apps_for(provider).clear(principal.org_id):
        raise HTTPException(
            404, f"This organisation has no {provider} app of its own."
        )
    return {"ok": True, "disconnected": _drop_connections(provider, principal.org_id)}


@app.get("/api/connections/google/setup")
def google_setup(principal: Principal = Depends(me)):
    status = google.setup_status(principal.org_id, _google_apps())
    conn = _google_connections().load(principal.org_id, "default")
    status["connected"] = conn is not None
    status["mailbox"] = (conn or {}).get("mailbox")
    return status


@app.post("/api/connections/google/start")
def start_google(principal: Principal = Depends(me)):
    principal.require("admin")
    try:
        url = google.start(STATES, principal.org_id, "default",
                           principal.email, _google_apps())
    except google.GoogleError as exc:
        raise HTTPException(500, str(exc)) from None
    return {"url": url}


@app.get("/api/connections/google/callback")
def google_callback(code: str | None = None, state: str | None = None,
                    error: str | None = None):
    if error:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error={error}")
    if not code or not state:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=missing_code")

    pending = STATES.take(state)
    if pending is None:
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=expired_state")

    try:
        token = google.exchange(code, pending["verifier"], pending["org_id"],
                                _google_apps())
        if not token.get("refresh_token"):
            # Without one, every run after the first hour would fail. Better to
            # refuse now than to store a connection that quietly expires.
            return RedirectResponse(f"{WEB_ORIGIN}/settings?error=no_refresh_token")
        token["mailbox"] = google.mailbox(token["access_token"])
        token["connected_by"] = pending["created_by"]
        _google_connections().save(pending["org_id"], "default", token)
    except (google.GoogleError, EncryptionError):
        return RedirectResponse(f"{WEB_ORIGIN}/settings?error=exchange_failed")

    return RedirectResponse(f"{WEB_ORIGIN}/settings?connected=gmail")


@app.delete("/api/connections/google")
def disconnect_google(principal: Principal = Depends(me)):
    principal.require("admin")
    if not _google_connections().disconnect(principal.org_id, "default"):
        raise HTTPException(404, "Gmail is not connected.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

@app.get("/api/workflows/{key}/templates")
def get_templates(key: str, principal: Principal = Depends(me)):
    if not ACCESS.has(principal.org_id, key):
        raise HTTPException(404, f"Unknown workflow {key!r}")
    module = workflows.get(key)
    defaults = _defaults_for(module)
    if not defaults:
        raise HTTPException(404, "This workflow has no editable templates.")
    return {
        "workflow": key,
        "placeholders": [
            {"token": tok, "description": desc}
            for tok, desc in getattr(module, "PLACEHOLDERS", [])
        ],
        "templates": TEMPLATES.merged(principal.org_id, key, defaults),
    }


class TemplateEdit(BaseModel):
    variant: str
    subject: str
    body: str


@app.put("/api/workflows/{key}/templates")
def save_template(key: str, body: TemplateEdit,
                  principal: Principal = Depends(me)):
    principal.require("member")
    if not ACCESS.has(principal.org_id, key):
        raise HTTPException(404, f"Unknown workflow {key!r}")
    defaults = _defaults_for(workflows.get(key))
    if body.variant not in defaults:
        raise HTTPException(422, f"Unknown template {body.variant!r}")
    if not body.body.strip():
        raise HTTPException(422, "The message body cannot be empty.")
    TEMPLATES.save(principal.org_id, key, body.variant,
                   body.subject, body.body, principal.email)
    return {"ok": True}


@app.delete("/api/workflows/{key}/templates/{variant}")
def reset_template(key: str, variant: str, principal: Principal = Depends(me)):
    principal.require("member")
    if not ACCESS.has(principal.org_id, key):
        raise HTTPException(404, f"Unknown workflow {key!r}")
    if not TEMPLATES.reset(principal.org_id, key, variant):
        raise HTTPException(404, "That template is not customised.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Workflow requests — approach 4: describe it, get a pull request
# ---------------------------------------------------------------------------

class WorkflowRequest(BaseModel):
    title: str
    description: str


@app.get("/api/workflow-requests")
def list_workflow_requests(principal: Principal = Depends(me)):
    from . import github

    return {
        "requests": REQUESTS.list(principal.org_id),
        # Configuration state, so the form can say up front what will happen
        # rather than failing after someone has written three paragraphs.
        "configured": github.configured(),
        "generation": requests_pr.generation_configured(),
    }


@app.post("/api/workflow-requests")
def create_workflow_request(body: WorkflowRequest,
                            principal: Principal = Depends(me)):
    principal.require("member")
    title = body.title.strip()
    description = body.description.strip()
    if not title or len(title) > 120:
        raise HTTPException(422, "A title is required, at most 120 characters.")
    if len(description) < 30:
        raise HTTPException(
            422, "Describe the workflow in at least a sentence or two — the "
                 "person building it has only this text to go on.")
    if len(description) > 8000:
        raise HTTPException(422, "The description is too long (8000 chars max).")

    request_id = REQUESTS.create(principal.org_id, title, description,
                                 principal.email)
    try:
        # Blocking on purpose, same as runs: generation plus the GitHub round
        # trips take up to a couple of minutes, and the requester should leave
        # this page knowing whether a PR exists, not wondering.
        outcome = POOL.submit(
            requests_pr.open_request_pr,
            title, description, principal.org_name, principal.email, request_id,
        ).result()
        REQUESTS.resolve(principal.org_id, request_id,
                         pr_url=outcome["pr_url"], kind=outcome["kind"],
                         error=None)
    except Exception as exc:  # noqa: BLE001 — the row carries the reason
        REQUESTS.resolve(principal.org_id, request_id,
                         pr_url=None, kind=None, error=str(exc)[:500])

    for r in REQUESTS.list(principal.org_id):
        if r["id"] == request_id:
            return r
    raise HTTPException(500, "The request was not recorded.")


@app.delete("/api/connections/{name}")
def disconnect(name: str, principal: Principal = Depends(me)):
    principal.require("admin")
    if not _connections().disconnect(principal.org_id, name):
        raise HTTPException(404, "No such connection")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Vendor onboarding
# ---------------------------------------------------------------------------

@app.post("/api/vendors/import")
def import_vendors(body: ImportVendors, principal: Principal = Depends(me)):
    """Load an org's vendor bank details, e.g. from their existing template."""
    principal.require("admin")
    count = VendorStore.replace_region(
        ENGINE, principal.org_id, body.region, body.records
    )
    if body.columns:
        LAYOUTS.set(principal.org_id, body.region, body.columns)
    return {"imported": count, "region": body.region}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@app.get("/api/runs")
def list_runs(principal: Principal = Depends(me)):
    return {"runs": RUNS.list(principal.org_id)}


@app.post("/api/runs")
def start_run(body: StartRun, principal: Principal = Depends(me)):
    principal.require("member")
    if not ACCESS.has(principal.org_id, body.workflow):
        # Deliberately the same answer as an unknown key: an org should not be
        # able to discover which workflows exist by probing.
        raise HTTPException(404, f"Unknown workflow {body.workflow!r}")
    try:
        module = workflows.get(body.workflow)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None

    try:
        params = module.SPEC.parse(body.params)
    except ParamError as exc:
        raise HTTPException(422, detail={"fieldErrors": exc.errors}) from None

    region = params.get("region")
    run_id = RUNS.create(principal.org_id, body.workflow, params, principal.email)

    ctx = RunContext(
        org_id=principal.org_id,
        creds=XeroCredentials(principal.org_id, _connections(), log=lambda m: None,
                              apps=_apps()),
        # Only region-based workflows need vendor banking data.
        bank_details=VendorStore(ENGINE, principal.org_id, region) if region else None,
        templates=TEMPLATES.merged(principal.org_id, body.workflow,
                                   _defaults_for(module)) or None,
        mailer=_mailer(principal.org_id),
        sender_name=principal.email.split("@")[0].replace(".", " ").title(),
        log=lambda m: None,
    )

    try:
        result = POOL.submit(module.run, params, ctx).result()
    except (XeroError, EncryptionError, ValueError) as exc:
        RUNS.fail(principal.org_id, run_id, str(exc), [])
        return _run_json(RUNS.get(principal.org_id, run_id), principal.org_id)
    except Exception as exc:  # noqa: BLE001
        RUNS.fail(principal.org_id, run_id, str(exc),
                  [traceback.format_exc(limit=3)])
        return _run_json(RUNS.get(principal.org_id, run_id), principal.org_id)

    RUNS.finish_pull(principal.org_id, run_id, result)
    return _run_json(RUNS.get(principal.org_id, run_id), principal.org_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(me)):
    run = RUNS.get(principal.org_id, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return _run_json(run, principal.org_id)


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str, body: Approve, principal: Principal = Depends(me)):
    principal.require("member")
    run = RUNS.get(principal.org_id, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run["status"] != RunStatus.NEEDS_APPROVAL.value:
        raise HTTPException(409, f"Run is {run['status']}, not awaiting approval.")

    wanted = set(body.rowIds)
    chosen = [r for r in run["rows"] if r["id"] in wanted]
    if not chosen:
        raise HTTPException(422, "No rows selected.")

    # A bank-file layout only applies to workflows that produce one. The
    # chase-up has no region and no template; demanding a layout would block it.
    region = run["params"].get("region")
    columns = []
    if region:
        columns = LAYOUTS.get(principal.org_id, region)
        if not columns:
            raise HTTPException(
                409,
                f"No bank file layout configured for {region}. Import it in Settings.",
            )

    module = workflows.get(run["workflow"])
    ctx = RunContext(
        org_id=principal.org_id,
        creds=None,
        templates=TEMPLATES.merged(principal.org_id, run["workflow"],
                                   _defaults_for(module)) or None,
        mailer=_mailer(principal.org_id),
        sender_name=principal.email.split("@")[0].replace(".", " ").title(),
        log=lambda m: None,
    )
    try:
        result = module.finalise(run["params"], chosen, ctx, columns)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not build the bank file: {exc}") from None

    RUNS.complete(principal.org_id, run_id, result,
                  [r["id"] for r in chosen], principal.email)
    return _run_json(RUNS.get(principal.org_id, run_id), principal.org_id)


@app.get("/api/runs/{run_id}/artifact")
def artifact(run_id: str, principal: Principal = Depends(me)):
    found = RUNS.artifact(principal.org_id, run_id)
    if found is None:
        raise HTTPException(404, "No file for this run")
    name, data = found
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
