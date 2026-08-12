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

from . import env, oauth, workflows

env.load()  # before anything reads os.environ

from .auth import AuthError, Principal, principal_from_request
from .contract import ParamError, RunContext, RunStatus
from .crypto import Cipher, EncryptionError
from .db import create_db_engine, init_db
from .stores import (
    ConnectionStore,
    LayoutStore,
    OAuthStateStore,
    OrgStore,
    RunStore,
    VendorStore,
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
    return {"workflows": [s.to_json() for s in workflows.specs()]}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@app.get("/api/connections")
def list_connections(principal: Principal = Depends(me)):
    return {"connections": _connections().list(principal.org_id)}


@app.get("/api/connections/xero/setup")
def xero_setup(principal: Principal = Depends(me)):
    """Configuration the Settings page needs to walk someone through connecting."""
    return oauth.setup_status()


@app.post("/api/connections/xero/start")
def start_xero(name: str = "default", principal: Principal = Depends(me)):
    principal.require("admin")
    try:
        url = oauth.start(STATES, principal.org_id, name, principal.email)
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
        token = oauth.exchange(code, pending["verifier"])
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
        creds=XeroCredentials(principal.org_id, _connections(), log=lambda m: None),
        bank_details=VendorStore(ENGINE, principal.org_id, region),
        log=lambda m: None,
    )

    try:
        result = POOL.submit(module.run, params, ctx).result()
    except (XeroError, EncryptionError, ValueError) as exc:
        RUNS.fail(principal.org_id, run_id, str(exc), [])
        return RUNS.get(principal.org_id, run_id)
    except Exception as exc:  # noqa: BLE001
        RUNS.fail(principal.org_id, run_id, str(exc),
                  [traceback.format_exc(limit=3)])
        return RUNS.get(principal.org_id, run_id)

    RUNS.finish_pull(principal.org_id, run_id, result)
    return RUNS.get(principal.org_id, run_id)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(me)):
    run = RUNS.get(principal.org_id, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


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

    region = run["params"].get("region")
    columns = LAYOUTS.get(principal.org_id, region)
    if not columns:
        raise HTTPException(
            409,
            f"No bank file layout configured for {region}. Import it in Settings.",
        )

    module = workflows.get(run["workflow"])
    ctx = RunContext(org_id=principal.org_id, creds=None, log=lambda m: None)
    try:
        result = module.finalise(run["params"], chosen, ctx, columns)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not build the bank file: {exc}") from None

    RUNS.complete(principal.org_id, run_id, result,
                  [r["id"] for r in chosen], principal.email)
    return RUNS.get(principal.org_id, run_id)


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
