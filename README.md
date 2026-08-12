# finance-workflows

A hosted version of the local finance desktop apps, for other finance teams.
One workflow implemented end-to-end — **Weekly Pay Run**, staging only, no
Wise write path — on a multi-tenant foundation.

## What it proves

1. A workflow is a Python module declaring `SPEC` + `run()` + `finalise()`.
   The launch form, review table and run history are generated from that
   declaration — adding workflow #2 touches no platform code.
2. The review step survives the move to the browser and gets better: a run
   pauses at `needs_approval` and the URL can go to whoever signs it off.
   `created_by` and `approved_by` are separate columns, and the database
   refuses to record a completed run without an approver.
3. Vendor bank details and bank-file layouts are per-org rows, not a
   spreadsheet on one person's OneDrive.
4. Every org's Xero token is encrypted at rest and readable by nobody —
   not even an admin of that org.

## Layout

```
migrations/        SQL applied to Supabase, in order
worker/            Python — runs the workflows. Needs a persistent host
  fw/contract.py     the workflow interface (ParamSpec, RunResult, RunContext)
  fw/db.py           schema + engine; SQLite locally, Postgres hosted
  fw/auth.py         Supabase JWT -> Principal (user, org, role)
  fw/crypto.py       Fernet envelope for stored tokens
  fw/stores.py       org-scoped data access — every query filters on org_id
  fw/oauth.py        Xero consent (PKCE), server-side
  fw/banking.py      BankDetailsSource: Excel (onboarding) | Vendor table
  fw/xero.py         token refresh + read-only Accounting API client
  fw/workflows/      one module per workflow, registered in __init__.py
  fw/seed.py         create an org and import an existing desktop setup
  fw/server.py       the API
web/               SvelteKit — sign-in, tiles, generated forms, review, settings
```

## Tenancy model

Identity comes from Supabase Auth. Authority does not: a valid JWT proves who
you are, and `org_members` decides what you may touch. The client may send
`X-Org-Id` to pick an org, but it is a request, not a grant — a non-member and
a non-existent org return the same 404, so the header cannot be used to probe.

Roles: `admin` connects integrations, `member` runs and approves, `viewer`
reads.

The worker connects as the Supabase service role, which **bypasses RLS**. So
the `where org_id = ...` clause in every `stores.py` query is the primary
control; a missing one is a security bug, not a style issue. RLS is the second
line of defence for anything holding a publishable key, and is verified to:

- show a member only their own org's rows across every table
- refuse `connections.secret` even to an admin of the owning org (column grant)
- refuse `oauth_states` to any browser session at all
- reject every insert/update/delete from a browser session, so a run cannot be
  self-approved outside the API

## Running it

```bash
cd worker && python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Create `worker/.env` (gitignored) with at least an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print('FW_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
```

Then seed an org. This is the onboarding path a customer takes, minus the UI:

```bash
cd worker && ./.venv/Scripts/python.exe -m fw.seed --email you@company.com --org yourco
```

Servers are defined in `../collectly/.claude/launch.json` as `fw-worker`
(:8000) and `fw-web` (:5174); Vite proxies `/api` to the worker.

```bash
cd worker && ./.venv/Scripts/python.exe -m pytest tests -q
```

## Deployment

Both halves live on Render, deployed from one blueprint (`render.yaml`) on one
`git push`.

| | |
| --- | --- |
| Web | https://finance-workflows-web.onrender.com |
| Worker | https://finance-workflows-worker.onrender.com |
| Database | Supabase `zacgedjltfkiyfydghtp`, ap-southeast-1 |

The web app is a **static SPA** (`@sveltejs/adapter-static`): every route sets
`ssr = false` and all data comes from the worker, so there is nothing for a
server to render -- and no Node runtime, which is what removed the
`adapter-vercel` "Unsupported Node.js version" build failure.

The static site rewrites `/api/*` to the worker. That single line is why there
is no CORS configuration, no cross-origin auth header handling, and no
per-environment API base URL: the browser only ever talks to one origin.
`API_BASE` in `src/lib/config.js` is empty and should stay that way unless the
two are ever split across hosts again.

The worker is a container, not a function, because a Xero pull outlives any
serverless timeout. On Render's free plan it sleeps after ~15 minutes idle, so
the first request after a quiet spell takes ~50s. `fly.toml` is kept for the
always-on alternative (~$2-5/month, no cold starts); the Dockerfile is the same.

`/health` returns `{"status":"ok"}` only after a `select 1` against the
database, so a green health check proves the worker reached Supabase.

## Environment

| Variable | Purpose |
| --- | --- |
| `FW_ENCRYPTION_KEY` | Fernet key for stored tokens. Required. |
| `FW_DATABASE_URL` | Unset = SQLite. Set to the Supabase Postgres URL to go hosted. |
| `FW_XERO_CLIENT_ID` / `_SECRET` | The **platform's** Xero app, shared by all orgs. |
| `FW_XERO_REDIRECT_URI` | Must match the Xero app's registered callback. |
| `SUPABASE_URL` or `FW_SUPABASE_JWT_SECRET` | Enables real JWT verification. |
| `FW_DEV_USER` / `FW_DEV_USER_ID` | Dev-only principal. Refuses to work unless `FW_ENV=dev`. |
| `PUBLIC_SUPABASE_URL` / `PUBLIC_SUPABASE_ANON_KEY` | Web app sign-in. Unset = dev mode. |
| `FW_GOOGLE_CLIENT_ID` / `_SECRET` | The platform's Google app (Gmail drafting). Orgs may override in Settings. |
| `FW_GOOGLE_REDIRECT_URI` | Must be registered on the Google client. |
| `FW_GITHUB_TOKEN` / `FW_GITHUB_REPO` | Lets "Request a workflow" open pull requests on this repo. |
| `FW_ANTHROPIC_API_KEY` / `FW_ANTHROPIC_MODEL` | Optional: attaches a generated draft module to request PRs. |

The Xero app belongs to the platform, not to each customer. In the desktop app
Peter registered his own Xero app and pasted its credentials into Settings;
asking a finance team to register a developer app would kill onboarding. Only
the token is per-org.

## Deliberately absent

- **The Wise write path.** Nothing here can move money. `finalise()` produces a
  CSV the customer uploads to their bank themselves.
- **Envelope encryption with a KMS.** One key from the environment, applied per
  row. `key_id` is stored with every ciphertext so rotation is a migration
  rather than a redesign, but this is not yet production-grade key management.
- **A real queue.** Runs execute in a thread pool. A Xero pull of a few hundred
  bills already exceeds a serverless timeout, which is why the worker cannot
  live on Vercel next to the web app.
- **Invites and org creation in the UI.** `fw.seed` does it from the CLI.
- **The Playwright enrichment step** from the chase-up app. It needs an
  interactive login and will not run headless.

## Verified against real data

A UK run pulled live Xero bills using a token decrypted from the database,
matched vendors against the imported vendor table, flagged one vendor with no
bank details, and produced a CSV whose columns match the org's stored bank-file
layout. A second org could not see the run, its rows, or its downloaded file.
