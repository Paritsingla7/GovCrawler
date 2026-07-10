# Architecture

GovCrawler is a **multi-user, cloud-split, RBAC** platform for discovering, crawling, and extracting
contact data from Indian government domains, plus an email-outreach system on top of the harvested leads.
The codebase is organized into three tiers plus a thin entry-point shim:

| Tier           | Package   | Runs where                   | Owns                                                                                                                        |
|----------------|-----------|------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| **Shared**     | `shared/` | imported by both other tiers | enums, permission catalog, wire DTOs, lead-scoring — the single source of truth for anything both tiers must agree on       |
| **Cloud**      | `cloud/`  | the VPS (Docker Compose)     | FastAPI app, auth/RBAC, the Postgres database of record, the SMTP dispatcher, the admin UI                                  |
| **Agent**      | `agent/`  | each operator's machine      | the crawler engine + parser, a durable local outbox/frontier, the Tkinter launcher, and a local BFF that talks to the cloud |
| **Entry shim** | `portal/` | both                         | `load_config()`, path resolution + first-run bootstrap, and the `python -m portal` CLI                                      |

> **Dependency direction:** `cloud → shared` and `agent → shared`; `agent ⊥ cloud` — **zero** imports in
> either direction (plan.md §19.1 Phase 9, both Parts complete) — enforced by two `import-linter` CI
> contracts (`pyproject.toml`). `shared/` imports neither tier. `portal/` (the entry-point shim) is the one
> place allowed to import both, since it's the composition root, not part of either tier's runtime.

---

## Deployment reality vs. target

Both are now the same thing — the module split *and* the process/deployment split are complete
(plan.md §19.1 Phase 9, Parts 1 and 2):

- **Cloud, containerized, crawler-free:** `deploy/docker-compose.yml` runs Postgres, a one-shot Alembic
  `migrate` service, the FastAPI `api` (a plain `python:3.11-slim` image — no Playwright, no Chromium, no
  `agent/` code), a standalone `dispatcher`, and a Caddy TLS `proxy`. `cloud/api/server.py` never imports
  `agent.*` and never touches Playwright; its lifespan only owns the stale-job reaper and stuck-`SENDING`
  recovery.
- **Agent, standalone, per operator machine:** `run.py` launches the Tkinter launcher
  (`agent/launcher/app.py`), which now boots its own **local BFF** (`agent/bff/app.py`) — a completely
  separate FastAPI app from the cloud's, bound to loopback. It owns Playwright/the crawl browser directly
  (moved out of the cloud process for good), renders the operator's pages itself
  (`agent/bff/pages.py`, from `frontend/agent/` + the tier-agnostic `frontend/shared/`), proxies every shared-data API
  call to the configured `cloud_api_base_url` (`agent/bff/proxy.py`, one generic reverse-proxy handler, not
  ~15 hand-written pass-throughs), and runs the crawl engine as an `asyncio.Task` in-process
  (`agent/api.py`'s job-lifecycle routes). The operator's browser only ever talks to this local origin —
  the real bearer token never reaches it, only a local `session`/`csrf` cookie pair
  (**⚠️ #58: the `/auth/login` relay currently still returns the raw access+refresh tokens in its JSON
  response body — the browser ignores them, but they should not be on the wire; pending fix**)
  (`agent/bff/security.py`). The Tkinter launcher itself is the sole authenticator: it logs in directly
  against the cloud (`agent/identity.py` — a self-refreshing token via `/auth/refresh` + the OS keyring)
  and hands the browser a ready-made session via `GET /local-bootstrap`, replacing the old same-process
  `/auth/bootstrap?token=` hand-off. `agent/localdb.py` holds this machine's own settings
  (`cloud_api_base_url`, a durable `agent_id`) and its own visited-URL recrawl-protection history — 100%
  local, never synced to the cloud (see [resilience.md](resilience.md)). The admin dashboard is
  deliberately **not** rendered by the agent at all — an admin-capable operator gets an external link that
  opens the cloud's own `/admin/dashboard` in a new tab, requiring its own separate login.
- **Dispatcher, independently deployable:** `dispatch.mode` (`embedded` vs `external`) decides whether the
  API process runs the SMTP send loop in-process or leaves it to the standalone `cloud/dispatch_service.py`.
  Unaffected by the agent/cloud split — dispatch always ran cloud-side only. See
  [outreach.md](outreach.md#dispatch-modes).

A crawl job may only ever be **resumed by the agent that started it** — `crawl_jobs.agent_hostname` stores
that agent's durable `agent_id`, and `cloud/api/coordination.py`'s resume route rejects any other agent
unconditionally (there is no frontier/visited data anywhere else to resume from). Jobs themselves stay
centrally recorded in the cloud DB for admin visibility; only the resume *action* is agent-exclusive.

---

## Trust model

There is exactly **one trust boundary: the cloud API.** Postgres binds to loopback on the VPS and is never
published; only the `migrate`/`api`/`dispatcher` containers reach it over the internal Compose network. No
client ever holds a database connection string — all shared-data access goes through an authenticated,
RBAC-checked HTTP API. A leaked agent cannot bypass a permission with raw SQL, and the database is never
exposed to the internet.

```
┌────── Operator machine (per user, N concurrent) ─────────────┐
│  Browser ──(loopback only, local session cookie)──┐           │
│                                                     ▼           │
│  Tkinter launcher (agent/launcher) — login + start/stop        │
│      │ boots                                                   │
│      ▼                                                          │
│  agent.bff.app — a standalone FastAPI app (NOT the cloud's)    │
│   ├─ pages.py    — renders frontend/agent/ + shared templates  │
│   ├─ proxy.py    — one generic reverse-proxy for shared data   │
│   ├─ local_auth / local_system — this machine's own concerns   │
│   ├─ agent/api.py (job create/resume/cancel)                   │
│   └─ CrawlerEngine (agent/crawler) as an asyncio.Task           │
│  Local SQLite: agent/localdb.py (settings, agent_id, visited    │
│    history) + agent/local_store.py (outbox + frontier)         │
│  OS keyring — refresh token + last email                       │
└───────────────────────────────┬────────────────────────────────┘
                                 │ HTTPS + Bearer JWT (httpx, server-side only —
                                 │ the browser never holds this token)
                                 ▼
┌──────────────────────────── VPS (cloud) ──────────────────────┐
│  Caddy — TLS (Let's Encrypt); :443 the only public port        │
│      ▼                                                          │
│  Cloud API (FastAPI + Uvicorn) — crawler-free, no Playwright   │
│   ├─ Auth / RBAC / ownership scoping / audit                   │
│   ├─ All shared-data endpoints + the admin dashboard            │
│   └─ Agent-coordination endpoints (/api/coordination/*)        │
│  Dispatcher (cloud.dispatch_service) — SMTP send + pacing      │
│      ▼                                                          │
│  Postgres (127.0.0.1) — shared data + users/roles/audit         │
└──────────────────────────────────────────────────────────────────┘
```

See [authentication.md](authentication.md) for the auth/RBAC internals and [deployment.md](deployment.md)
for the container topology.

---

## The two databases

| Data                                                                                     | Location                                       |
|------------------------------------------------------------------------------------------|------------------------------------------------|
| users, roles, permissions (+ per-user overrides), sessions, audit log                    | **Cloud** (Postgres, or SQLite in desktop/dev) |
| categories, org_types, domains, crawl_jobs, snapshots, job_custom_urls                   | **Cloud**                                      |
| leads, lead_occurrences (shared pool)                                                    | **Cloud**                                      |
| campaigns, campaign_emails, credentials, templates, blacklist                            | **Cloud**                                      |
| this machine's settings (`cloud_api_base_url`, `agent_id`) + visited-URL recrawl history | **Local SQLite** (`agent/localdb.py`)          |
| per-job outbox + frontier checkpoint                                                     | **Local SQLite** (`agent/local_store.py`)      |
| refresh token + last-login email                                                         | **OS keyring**                                 |

The cloud DB is one SQLAlchemy database (`cloud.db.Database`, composed from several mixins). It runs on
**Postgres** in production and **SQLite** for desktop/dev — `database.uri` picks which. Neither local store
uses SQLAlchemy/Alembic (both are plain `sqlite3` with a `PRAGMA user_version` stepper where one is needed)
because they hold per-machine data, not shared schema — and, as of plan.md §19.1 Phase 9 Part 2, **nothing
in them is ever synced to the cloud**: visited-URL history and frontier checkpoints are this machine's
business alone, unlike leads (the only thing that flows outward) and job records (created/read centrally,
but never containing another agent's crawl state). See [database-schema.md](database-schema.md).

---

## Subsystems

### 1. Launcher — `agent/launcher/` + `run.py`

The desktop entry point, shipped as a PyInstaller `.exe` for Windows/macOS/Linux
(`.github/workflows/release.yaml`). `run.py` is a thin bootstrap (SSL cert fix, the `INSTALL_BROWSERS`
argv sentinel that installs Chromium via a subprocess, no-console stdio guard). `agent/launcher/app.py`
is `CrawlerLauncher`, an explicit `AppState` state machine (`IDLE → STARTING → RUNNING → CHECKING →
CANCELLING → DRAINING → STOPPING`) that:

- on first run, prompts for the **cloud server URL** (`agent/localdb.py`'s `cloud_api_base_url`); a
  "Cloud Server" panel in the main window shows this URL plus a live-polled (every 15 s) reachability
  indicator (`GET {url}/healthz`) and a **Change…** button — disabled while the server is running, since
  swapping the cloud mid-session would invalidate the current login/identity;
- starts/stops its own standalone `agent.bff.app` on a daemon thread (so Tkinter's mainloop stays
  responsive);
- shows a **login dialog**, authenticates directly against the cloud's `/auth/login` (not its own local
  server), and stores the refresh token in the OS **keyring** (access token in memory + `agent/identity.py`);
  auto-refreshes on 401 against the cloud directly too;
- polls its own local `GET /api/system/activity` every 1.5 s and toasts on job completion (campaign
  activity isn't polled here — dispatch never runs on the agent);
- opens the browser via the agent's own `GET /local-bootstrap` (a local session cookie hand-off, replacing
  the old same-process `/auth/bootstrap?token=`);
- on stop, runs a **confirm → cancel-all → drain** shutdown (up to a 180 s deadline) before killing Uvicorn.

`tray.py` runs a `pystray` icon on its own thread; `notifications.py` wraps cross-platform `notifypy`
toasts (deliberately **not** `winotify`, which imports Windows-only `winreg` and would crash on
macOS/Linux). Because the Tkinter thread and the Uvicorn event loop are different OS threads, the launcher
never touches asyncio task dicts directly — it drives the server over loopback HTTP, exactly like the
browser UI.

### 2. Cloud API — `cloud/api/`

A FastAPI app built by `create_app(config, db)` in `cloud/api/server.py`. It mounts ~17 routers and a
background **stale-job reaper** + stuck-`SENDING` recovery lifespan, configures CORS (only if
`auth.admin_origin` is set) and double-submit CSRF, and serves a Jinja2 browser UI
(`frontend/cloud/`, plus the shared `frontend/shared/` login page) via `cloud/api/frontend.py`. It never
imports `agent.*` and never touches Playwright — the crawler
is entirely the agent's concern. Every `/api/*` router carries `get_current_user` + `verify_csrf`; mutating
routes add a `require(<permission>)` dependency and write an audit-log row. Two error handlers
(`RequestValidationError`, catch-all `Exception`) normalize every error response to a plain-string `detail`
(`shared/errors.py`), so the frontend never has to render FastAPI's raw `[{loc, msg, type}, ...]` validation
array or a traceback. See [api-reference.md](api-reference.md).

### 3. Agent local BFF — `agent/bff/`

A second, completely independent FastAPI app (`agent/bff/app.py`), bound to loopback, that the launcher
boots instead of the cloud's. Owns Playwright/the crawl browser (its own lifespan starts/stops Chromium).
Composed of: `pages.py` (renders `frontend/agent/` + the shared `frontend/shared/` templates locally — zero
`cloud.*` import), `proxy.py` (one generic reverse-proxy for every shared-data route:
domains/leads/campaigns/templates/credentials/blacklist/imports/config/read-only jobs — forwards to
`cloud_api_base_url` with the operator's bearer token, built on the same retry-on-401-then-refresh helper
`agent/cloud_client.py` uses for coordination calls; a `ConnectError`/`TimeoutException` from the cloud is
caught and re-raised as a `CloudUnreachableError`, giving the frontend a distinct `code: "cloud_unreachable"`
instead of a generic 5xx), `local_auth.py` (the browser's own local `session`/`csrf` cookie pair — never the
real bearer token — plus a straight `/auth/login` relay so `frontend/shared/templates/login.html` keeps
working unmodified if the operator ever lands there directly), and `local_system.py` (this machine's own
`/api/system/activity` + `/api/system/cancel-all` + `/api/logs`, reading `agent/state.py`'s local task
registry — there is nothing else to read, since the cloud process no longer runs any crawl engine).
`security.py` is the floor under all of it: `require_loopback` (checks the actual peer address, not just the
bind host), `require_local_session` + `verify_local_csrf` (double-submit CSRF **and** a trusted-`Host`-header
check, defending against DNS-rebinding against this loopback service). `agent/api.py`'s job-lifecycle routes
(create/resume/cancel) mount into this same app, behind the same guards. The same
`RequestValidationError`/catch-all `Exception` normalization as the cloud tier applies here too.

### 4. Crawler engine — `agent/crawler/engine.py`

`CrawlerEngine` runs as an `asyncio.Task` on the agent BFF's event loop. Worker coroutines consume from an
`asyncio.PriorityQueue`; HTML parsing runs on a `parse_pool` (`ThreadPoolExecutor`, `cpu_count` threads) and
all persistence runs on a single-thread `db_pool`. Fetching is **HTTPX-first, Playwright-fallback**. Leads
are written to the durable local **outbox** (never straight to the cloud), which an async flusher drains to
the coordination API. Visited URLs go straight into `agent/localdb.py`'s local-only history (never
outboxed/synced anywhere — recrawl protection is this machine's business alone). A frontier checkpoint is
saved every 5 s for exact resume, 100% local. See [crawler.md](crawler.md) and [resilience.md](resilience.md).

### 5. SMTP dispatcher — `cloud/api/dispatcher.py` + `cloud/dispatch_service.py`

`run_campaign_dispatch()` is an async loop that claims `QUEUED` emails one at a time (atomically flipping
them to `SENDING` for at-most-once recovery), paces sends per SMTP credential, rotates round-robin over the
campaign's credential pool, and auto-blacklists hard bounces. In `embedded` mode the API process runs it; in
`external` mode the standalone `cloud/dispatch_service.py` polls for `RUNNING` campaigns and runs it instead.
See [outreach.md](outreach.md).

### 6. Lead scoring — `shared/scoring.py`

A pure function, `compute_lead_score()`, called wherever a lead is written or edited. Produces a 0–100 score
from email confidence band plus the presence of name/designation/phone, weighted by `lead_score.weights`
(DB-backed via `app_settings`, plan.md §19.1 Phase 8). Manual (CSV) leads always score 0.
`Database.recompute_lead_scores()` re-runs it for every lead when a weight change is posted to
`POST /api/config` (background task, not on every startup), so the change applies retroactively without a
restart. See [configuration.md](configuration.md#lead-scoring).

### 7. Domain discovery — `GovScraper/` + `cloud/services/importer.py`

`GovScraper/` is a standalone dev-time CLI that reads the `india.gov.in` Web Directory API (no browser, no
CAPTCHA) and emits `gov_domains.json`; it has no runtime dependency on `cloud`/`agent`/`shared`.
`cloud/services/importer.py` imports that JSON (or hits the live API directly, with the same API-calling
code inlined — Phase 7, plan.md §19.1) into the `domains` catalog, keeping organizations with no listed URL
as "not crawlable" rather than dropping them.

### 8. Frontend — `frontend/{shared,agent,cloud}/`

Three structurally separate trees, not just naming conventions, so it's never ambiguous which tier owns a
template or asset (see [directory-structure.md](directory-structure.md) for the full layout):

- **`frontend/shared/`** — the one page genuinely identical on both tiers (`login.html`), CSS design tokens
    + generic components (buttons, tables, modals, badges, the sidebar-tab-nav pattern), and the shared error
      layer: `http.js` (`apiFetch`/`ApiError`/`friendlyMessage` + the CSRF-injecting `fetch` patch) and
      `toast.js` (`showToast`/`showApiError`) — every raw `alert()` that used to report an API error is now a
      dismissible, human-readable toast; `confirm()` stays native for destructive-action confirmations, which
      isn't an error-reporting concern.
- **`frontend/agent/`** — the full crawler + outreach UI (dashboard, leads, campaigns, settings,
  test-campaign, user guide), rendered only by `agent/bff/pages.py`. Its only admin-adjacent affordance is a
  permission-gated "Admin Portal ↗" nav button that opens the cloud's own login in a new tab — a link-out,
  never rendered admin UI.
- **`frontend/cloud/`** — rendered by `cloud/api/frontend.py`. The admin surface is a single
  sidebar-tabbed `admin-dashboard.html` (Overview / Users & Permissions / Roles — read-only, since the
  backend only supports the 3 built-in roles / Audit Log / System — DB health + dispatch mode +
  `GET /api/admin/system-status`'s per-agent job-activity summary) plus a short `admin-guide.html`.
  **⚠️ Correction (issue #58): the cloud tier is NOT admin-only.** `frontend/cloud/templates/` also ships
  `leads.html`, `campaigns.html`, and `access-denied.html`, each wired to its own `leads.js`/`campaigns.js`
  under `frontend/cloud/static/js/` — near-duplicates (~600 lines) of the agent tree's versions, maintained
  by hand. The clean "crawler/outreach UI lives only on the agent" split described here is aspirational, not
  the current reality; the duplication (and the fact that the XSS-escaping fix landed only in the cloud copy)
  is tracked in #58.
- Both tiers mount `/static` to their own tree and `/assets` to `frontend/shared/static` (distinct
  prefixes, not nested, so there's no `StaticFiles` mount-order ambiguity); `Jinja2Templates` is built from
  a two-entry search path (tier-specific dir first, then `frontend/shared/templates`).

---

## Async / threading model

There are now two separate event loops in two separate processes — the cloud's and each agent's — each
with its own version of this model:

```
Cloud event loop (main thread)                    Agent BFF event loop (main thread, per machine)
  ├── FastAPI request handlers                       ├── FastAPI request handlers (pages/proxy/local_*)
  ├── reap loop (stale jobs, every 60 s)              ├── CrawlerEngine worker coroutines (× workers)
  └── SMTP dispatcher coroutine (embedded mode)       ├── CrawlerEngine _reporter (2 s) + _checkpoint_loop (5 s)
                                                       └── CloudApiClient outbox flush loop (leads only)

                                                     ThreadPoolExecutor: db_pool (1 thread) ← local persistence
                                                     ThreadPoolExecutor: parse_pool (cpu_count) ← parsing off the loop
```

**Never call blocking I/O directly in an `async` function on the event loop.** Use `asyncio.to_thread()`
for one-offs or submit to the appropriate executor.

---

## Data flow

### Crawl job

```
Browser → POST /api/jobs (agent/bff, loopback + local session + CSRF)
  → create_remote_job → POST /api/coordination/jobs (cloud, over the network)
      cloud: create crawl_jobs row (stamped with this agent's agent_id),
             freeze each seed into crawl_snapshots, load policy
      → {job_id, seeds, policy}
  agent: compute visited_bootstrap LOCALLY (agent/localdb.py's own recrawl
         history, minus this job's seed domains) — never asks the cloud
  → CrawlerEngine.run(seeds, visited_bootstrap)  [asyncio.Task on the agent's loop]
      worker: fetch (httpx → playwright fallback) → parse (parse_pool)
              → leads → local OUTBOX (db_pool) · visited URL → agent/localdb.py directly (never outboxed)
      flusher: OUTBOX → POST /api/coordination/jobs/{id}/leads (idempotent enrich-dedup)
      reporter: POST /api/coordination/jobs/{id}/heartbeat every 2 s → {cancel_requested}
      checkpoint: frontier snapshot every 5 s — 100% local, never uploaded
  → drain outbox → POST /api/coordination/jobs/{id}/finish

Resume (same agent only): POST /api/jobs/{id}/resume → resume_remote_job sends this
  agent's agent_id → cloud rejects with 403 if a different agent already owns the job
  → local frontier reloaded from agent/local_store.py → CrawlerEngine.run(seeds, frontier=...)
```

### Campaign dispatch

```
Browser → POST /api/campaigns (require campaigns.manage)
      render Jinja2 drafts per lead (blacklist-filtered) → campaign_emails (DRAFT), campaign starts PAUSED
Browser → review/deselect, assign credential pool → POST /api/campaigns/{id}/dispatch (require campaigns.dispatch)
      campaign → RUNNING; selected DRAFT → QUEUED
run_campaign_dispatch loop (embedded API task, or external dispatch_service):
      claim QUEUED→SENDING (atomic) → pace per credential → aiosmtplib send
        success → SENT · hard bounce (550/553) → blacklist + FAILED
        rate limit (421/450/451) → cooldown+retry · auth fail → disable credential
        no usable credential → campaign PAUSED (pause_reason)
      → COMPLETED (or PAUSED if deselected drafts remain)
```

### Domain import

```
Browser/CLI → POST /api/import/json (upload) or /api/import (live API)   [require domains.import]
  single-flight asyncio.Lock → asyncio.to_thread(import_from_json | import_all)
    clear_domains() → upsert_category/upsert_org_type/upsert_domain × N
    entries with no crawlable URL kept as main_url=None ("not crawlable")
  import_status dict updated in place (polled by GET /api/import/status)
```

### Login

```
Launcher → POST {cloud_api_base_url}/auth/login (directly against the cloud, not the local BFF)
  verify argon2id, check is_active/locked_until → issue access JWT (~15 min) + refresh token (~14 d, hashed in user_sessions)
  launcher: Bearer token cached in agent/identity.py, refresh token in OS keyring
  audit user.login
Launcher → GET {agent_base_url}/local-bootstrap (loopback)
  hands the browser a local session + csrf cookie pair — the real bearer token never reaches it
  (⚠️ except in the login relay's response body today — see the Trust model note above, issue #58)
Browser (fallback, if it ever lands on /login directly) → POST {agent_base_url}/auth/login
  agent/bff/local_auth.py relays to the cloud's real /auth/login, same effect as above
```

See [authentication.md](authentication.md) for refresh rotation, reuse detection, and revocation.

---

## Key design decisions

| Decision                                                                  | Rationale                                                                                                                                                                                                                  |
|---------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| One trust boundary (the API), Postgres loopback-only                      | RBAC is meaningless if clients hold DB connection strings; the API is the only place permissions/ownership can be enforced                                                                                                 |
| Durable local outbox as the primary write path                            | A cloud blip or crash never loses an extracted lead; idempotent cloud writes make at-least-once retry safe                                                                                                                 |
| Frontier checkpoint every 5 s, 100% local, no cloud copy                  | Interrupted crawls resume exactly from a checkpoint, not a full re-crawl — and since a job can only be resumed by the agent that started it, there's nothing to gain from a cloud-side copy                                |
| Visited-URL recrawl history is per-agent, not shared                      | Simpler and more honest than a shared table implied cross-agent coordination that never actually existed at job-execution granularity; the tradeoff (two agents might both re-crawl a recently-visited domain) is accepted |
| Heartbeat + stale-job reaper                                              | A silent agent is reaped to `interrupted` (resumable), never left as a phantom `running`; late heartbeats revive it non-destructively                                                                                      |
| At-most-once send (`SENDING` claim before the SMTP call)                  | A crash mid-send is reconciled as `failed` for manual review, never blindly re-sent — double-mailing officials wrecks sender reputation                                                                                    |
| Global `UNIQUE(email)` leads with enrich-on-conflict + `lead_occurrences` | One shared lead pool deduped by email, but per-job attribution and truthful per-job counts survive dedup                                                                                                                   |
| Leads read frozen `crawl_snapshots`, not live `domains`                   | Domain re-imports rebuild `domains` (reassigning ids); freezing seed metadata per crawl decouples leads from catalog churn                                                                                                 |
| Enums stored as `TEXT` + app-level `Enum`, not native PG `ENUM`           | `ALTER TYPE` is transaction-hostile and can't drop values; text keeps future migrations cheap                                                                                                                              |
| HTTPX-first, Playwright fallback                                          | ~60–70% of `.gov.in` sites are static HTML; skipping Playwright for those cuts crawl time sharply                                                                                                                          |
| Single-thread `db_pool` on the agent                                      | Serializes local-SQLite writes without transactions; the cloud DB uses connection pooling instead                                                                                                                          |
| Dispatcher `embedded` vs `external`                                       | Desktop runs it in-process; the VPS runs it as its own container so an API restart never kills in-flight sends                                                                                                             |
