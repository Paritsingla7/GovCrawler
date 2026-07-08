# Architecture

GovCrawler is a **multi-user, cloud-split, RBAC** platform for discovering, crawling, and extracting
contact data from Indian government domains, plus an email-outreach system on top of the harvested leads.
The codebase is organized into three tiers plus a thin entry-point shim:

| Tier | Package | Runs where | Owns |
|------|---------|-----------|------|
| **Shared** | `shared/` | imported by both other tiers | enums, permission catalog, wire DTOs, lead-scoring — the single source of truth for anything both tiers must agree on |
| **Cloud** | `cloud/` | the VPS (Docker Compose) | FastAPI app, auth/RBAC, the Postgres database of record, the SMTP dispatcher, the admin UI |
| **Agent** | `agent/` | each operator's machine | the crawler engine + parser, a durable local outbox/frontier, the Tkinter launcher, and a local BFF that talks to the cloud |
| **Entry shim** | `portal/` | both | `load_config()`, path resolution + first-run bootstrap, and the `python -m portal` CLI |

> **Dependency direction:** `cloud → shared` and `agent → shared`, never `cloud ↔ agent` — with one
> deliberate, flagged exception documented below. `shared/` imports neither tier.

---

## Deployment reality vs. target

The **module split is complete** (`portal/` no longer contains any `api/`, `db/`, or `crawler/` code —
those live under `cloud/` and `agent/`). The **process/deployment split is partial**, by design:

- **Cloud, containerized:** `deploy/docker-compose.yml` runs Postgres, a one-shot Alembic `migrate`
  service, the FastAPI `api`, a standalone `dispatcher`, and a Caddy TLS `proxy`. This is the recommended
  production path.
- **Desktop, single process:** `run.py` launches the Tkinter launcher, which starts **one** Uvicorn
  process hosting the cloud FastAPI app *and* the agent's crawl routes, and self-calls its own coordination
  API over loopback. The crawler is not yet a physically separate process/port; `agent/api.py` still
  imports a few `cloud.*` symbols directly (JWT minting, `DATA_DIR`). These residual couplings are marked in
  code as intentional, not-yet-closed gaps — the design boundary they will eventually follow is the HTTP
  coordination contract in `cloud/api/coordination.py`.
- **Dispatcher, independently deployable:** `dispatch.mode` (`embedded` vs `external`) decides whether the
  API process runs the SMTP send loop in-process or leaves it to the standalone `cloud/dispatch_service.py`.
  See [outreach.md](outreach.md#dispatch-modes).

---

## Trust model

There is exactly **one trust boundary: the cloud API.** Postgres binds to loopback on the VPS and is never
published; only the `migrate`/`api`/`dispatcher` containers reach it over the internal Compose network. No
client ever holds a database connection string — all shared-data access goes through an authenticated,
RBAC-checked HTTP API. A leaked agent cannot bypass a permission with raw SQL, and the database is never
exposed to the internet.

```
┌──────────────── Operator machine (per user) ────────────────┐
│  Tkinter launcher (agent/launcher) — login + start/stop      │
│      │ starts Uvicorn on a daemon thread                     │
│      ▼                                                        │
│  FastAPI app (cloud.api.server.create_app)                   │
│   ├─ cloud routers (auth, leads, campaigns, admin, …)        │
│   ├─ agent routes  (agent/api.py: create/resume/cancel job)  │
│   └─ CrawlerEngine (agent/crawler) as an asyncio.Task        │
│  Local SQLite (agent/local_store) — outbox + frontier only   │
│  OS keyring — refresh token + last email                     │
└───────────────────────────────┬──────────────────────────────┘
                                 │ HTTPS + Bearer JWT (httpx)
                                 ▼
┌──────────────────────────── VPS (cloud) ─────────────────────┐
│  Caddy — TLS (Let's Encrypt); :443 the only public port      │
│      ▼                                                        │
│  Cloud API (FastAPI + Uvicorn)                               │
│   ├─ Auth / RBAC / ownership scoping / audit                 │
│   ├─ All shared-data endpoints                               │
│   └─ Agent-coordination endpoints (/api/coordination/*)      │
│  Dispatcher (cloud.dispatch_service) — SMTP send + pacing    │
│      ▼                                                        │
│  Postgres (127.0.0.1) — shared data + users/roles/audit      │
└───────────────────────────────────────────────────────────────┘
```

See [authentication.md](authentication.md) for the auth/RBAC internals and [deployment.md](deployment.md)
for the container topology.

---

## The two databases

| Data | Location |
|------|----------|
| users, roles, permissions, sessions, audit log | **Cloud** (Postgres, or SQLite in desktop/dev) |
| categories, org_types, domains, crawl_jobs, snapshots, job_custom_urls, job_frontiers | **Cloud** |
| leads, lead_occurrences, visited_urls (shared pool) | **Cloud** |
| campaigns, campaign_emails, credentials, templates, blacklist | **Cloud** |
| per-machine outbox + frontier checkpoint | **Local SQLite** (`agent/local_store.py`) |
| refresh token + last-login email | **OS keyring** |

The cloud DB is one SQLAlchemy database (`cloud.db.Database`, composed from seven mixins). It runs on
**Postgres** in production and **SQLite** for desktop/dev — `database.uri` picks which. The local store is
plain `sqlite3` (deliberately not SQLAlchemy/Alembic) because it is a disposable per-machine resilience
buffer, not part of the shared schema. See [database-schema.md](database-schema.md).

---

## Subsystems

### 1. Launcher — `agent/launcher/` + `run.py`

The desktop entry point, shipped as a PyInstaller `.exe` for Windows/macOS/Linux
(`.github/workflows/release.yaml`). `run.py` is a thin bootstrap (SSL cert fix, the `INSTALL_BROWSERS`
argv sentinel that installs Chromium via a subprocess, no-console stdio guard). `agent/launcher/app.py`
is `CrawlerLauncher`, an explicit `AppState` state machine (`IDLE → STARTING → RUNNING → CHECKING →
CANCELLING → DRAINING → STOPPING`) that:

- starts/stops Uvicorn on a daemon thread (so Tkinter's mainloop stays responsive);
- shows a **login dialog**, authenticates against `/auth/login`, and stores the refresh token in the OS
  **keyring** (access token in memory); auto-refreshes on 401;
- polls `GET /api/system/activity` every 1.5 s and toasts on job/campaign completion;
- on stop, runs a **confirm → cancel-all → drain** shutdown (up to a 180 s deadline) before killing Uvicorn.

`tray.py` runs a `pystray` icon on its own thread; `notifications.py` wraps cross-platform `notifypy`
toasts (deliberately **not** `winotify`, which imports Windows-only `winreg` and would crash on
macOS/Linux). Because the Tkinter thread and the Uvicorn event loop are different OS threads, the launcher
never touches asyncio task dicts directly — it drives the server over loopback HTTP, exactly like the
browser UI.

### 2. Cloud API — `cloud/api/`

A FastAPI app built by `create_app(config, db)` in `cloud/api/server.py`. It mounts ~15 routers, sets up a
lifespan that owns the shared Playwright browser and a background **stale-job reaper**, configures CORS
(only if `auth.admin_origin` is set) and double-submit CSRF, and serves the Jinja2 browser UI from
`cloud/frontend/`. Every `/api/*` router carries `get_current_user` + `verify_csrf`; mutating routes add a
`require(<permission>)` dependency and write an audit-log row. See [api-reference.md](api-reference.md).

### 3. Crawler engine — `agent/crawler/engine.py`

`CrawlerEngine` runs as an `asyncio.Task` on the Uvicorn event loop. Worker coroutines consume from an
`asyncio.PriorityQueue`; HTML parsing runs on a `parse_pool` (`ThreadPoolExecutor`, `cpu_count` threads) and
all persistence runs on a single-thread `db_pool`. Fetching is **HTTPX-first, Playwright-fallback**. Leads
and visited URLs are written to the durable local **outbox** (never straight to the cloud), which an async
flusher drains to the coordination API. A frontier checkpoint is saved every 5 s for exact resume. See
[crawler.md](crawler.md) and [resilience.md](resilience.md).

### 4. SMTP dispatcher — `cloud/api/dispatcher.py` + `cloud/dispatch_service.py`

`run_campaign_dispatch()` is an async loop that claims `QUEUED` emails one at a time (atomically flipping
them to `SENDING` for at-most-once recovery), paces sends per SMTP credential, rotates round-robin over the
campaign's credential pool, and auto-blacklists hard bounces. In `embedded` mode the API process runs it; in
`external` mode the standalone `cloud/dispatch_service.py` polls for `RUNNING` campaigns and runs it instead.
See [outreach.md](outreach.md).

### 5. Lead scoring — `shared/scoring.py`

A pure function, `compute_lead_score()`, called wherever a lead is written or edited. Produces a 0–100 score
from email confidence band plus the presence of name/designation/phone, weighted by `lead_score.weights`.
Manual (CSV) leads always score 0. `Database._recompute_lead_scores()` re-runs it for every lead on each
startup, so a weight change applies retroactively. See [configuration.md](configuration.md#lead-scoring).

### 6. Domain discovery — `GovScraper/` + `cloud/scraper/importer.py`

`GovScraper/` is a standalone tool that reads the `india.gov.in` Web Directory API (no browser, no CAPTCHA)
and emits `gov_domains.json`. `cloud/scraper/importer.py` imports that JSON (or hits the live API) into the
`domains` catalog, keeping organizations with no listed URL as "not crawlable" rather than dropping them.

---

## Async / threading model

```
Uvicorn event loop (main thread)
  ├── FastAPI request handlers
  ├── CrawlerEngine worker coroutines (× workers)
  ├── CrawlerEngine _reporter (heartbeat every 2 s) + _checkpoint_loop (every 5 s)
  ├── CloudApiClient outbox flush loop
  └── SMTP dispatcher coroutine (embedded mode) + reaper loop

ThreadPoolExecutor: db_pool   (1 thread)      ← serialized local persistence / checkpoints
ThreadPoolExecutor: parse_pool (cpu_count)    ← BeautifulSoup parsing off the loop
asyncio.to_thread                              ← blocking domain imports
```

**Never call blocking I/O directly in an `async` function on the event loop.** Use `asyncio.to_thread()`
for one-offs or submit to the appropriate executor. The one place a *synchronous* HTTP call is allowed is
`CloudApiClient.save_frontier`'s optional cloud upload — it runs on the `db_pool` thread, not the loop.

---

## Data flow

### Crawl job

```
Browser → POST /api/jobs (agent/api.py, require crawl.run)
  → create_remote_job → POST /api/coordination/jobs (cloud)
      cloud: create crawl_jobs row, freeze each seed into crawl_snapshots,
             compute seed-scoped visited_bootstrap, load policy
      → {job_id, seeds, policy, visited_bootstrap}
  → CrawlerEngine.run(seeds)  [asyncio.Task on the loop]
      worker: fetch (httpx → playwright fallback) → parse (parse_pool)
              → leads/visited → local OUTBOX (db_pool)
      flusher: OUTBOX → POST /api/coordination/jobs/{id}/leads|visited (idempotent enrich-dedup)
      reporter: POST /api/coordination/jobs/{id}/heartbeat every 2 s → {cancel_requested}
      checkpoint: frontier snapshot every 5 s (local; + cloud if cross_machine_resume)
  → drain outbox → POST /api/coordination/jobs/{id}/finish
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
Launcher/browser → POST /auth/login
  verify argon2id, check is_active/locked_until → issue access JWT (~15 min) + refresh token (~14 d, hashed in user_sessions)
  browser: httpOnly Secure SameSite cookies + CSRF cookie · launcher: Bearer token, refresh in keyring
  audit user.login
```

See [authentication.md](authentication.md) for refresh rotation, reuse detection, and revocation.

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| One trust boundary (the API), Postgres loopback-only | RBAC is meaningless if clients hold DB connection strings; the API is the only place permissions/ownership can be enforced |
| Durable local outbox as the primary write path | A cloud blip or crash never loses an extracted lead; idempotent cloud writes make at-least-once retry safe |
| Frontier checkpoint every 5 s | Interrupted crawls resume exactly from a checkpoint, not from a full re-crawl of seeds |
| Heartbeat + stale-job reaper | A silent agent is reaped to `interrupted` (resumable), never left as a phantom `running`; late heartbeats revive it non-destructively |
| At-most-once send (`SENDING` claim before the SMTP call) | A crash mid-send is reconciled as `failed` for manual review, never blindly re-sent — double-mailing officials wrecks sender reputation |
| Global `UNIQUE(email)` leads with enrich-on-conflict + `lead_occurrences` | One shared lead pool deduped by email, but per-job attribution and truthful per-job counts survive dedup |
| Leads read frozen `crawl_snapshots`, not live `domains` | Domain re-imports rebuild `domains` (reassigning ids); freezing seed metadata per crawl decouples leads from catalog churn |
| Enums stored as `TEXT` + app-level `Enum`, not native PG `ENUM` | `ALTER TYPE` is transaction-hostile and can't drop values; text keeps future migrations cheap |
| HTTPX-first, Playwright fallback | ~60–70% of `.gov.in` sites are static HTML; skipping Playwright for those cuts crawl time sharply |
| Single-thread `db_pool` on the agent | Serializes local-SQLite writes without transactions; the cloud DB uses connection pooling instead |
| Dispatcher `embedded` vs `external` | Desktop runs it in-process; the VPS runs it as its own container so an API restart never kills in-flight sends |
