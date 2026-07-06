# Architecture

## High-Level Overview

GovCrawler is a monolithic Python application composed of four logically distinct subsystems that share a single FastAPI
process and SQLite/PostgreSQL database.

```
┌──────────────────────────────────────────────────────────┐
│                    GovCrawler Process                    │
│                                                          │
│  ┌─────────────┐   ┌─────────────────────────────────┐  │
│  │ Tkinter GUI  │   │        FastAPI / Uvicorn         │  │
│  │ (run.py +   │──▶│  (portal/api/server.py)          │  │
│  │  launcher/) │◀──│                                  │  │
│  └──────┬──────┘   │  Frontend routes  API routes      │  │
│         │          │  /  /leads        /api/domains    │  │
│  ┌──────▼──────┐   │  /campaigns       /api/jobs       │  │
│  │ Tray + Toast │   │  /settings        /api/leads      │  │
│  │ (pystray,   │   │                   /api/campaigns  │  │
│  │  notifypy)  │   │                   /api/system     │  │
│  └─────────────┘   └──────────┬──────────────┬─────────┘  │
│                               │              │            │
│                    ┌──────────▼──────┐  ┌────▼─────────┐ │
│                    │  CrawlerEngine  │  │  SMTP        │ │
│                    │  (engine.py)    │  │  Dispatcher  │ │
│                    │                 │  │  (dispatcher │ │
│                    │  HTTPX + PW     │  │   .py)       │ │
│                    └──────────┬──────┘  └────┬─────────┘ │
│                               │              │            │
│                    ┌──────────▼──────────────▼─────────┐ │
│                    │          Database (SQLAlchemy)     │ │
│                    │  SQLite (default) / PostgreSQL     │ │
│                    └───────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## Subsystems

### 1. Control Plane — `run.py` + `launcher/` (Tkinter GUI)

The entry point for end users running the packaged executable — built and shipped for Windows, macOS, and Linux
(`.github/workflows/release.yaml`). `run.py` itself is a thin bootstrap (SSL/cert fixes, the `INSTALL_BROWSERS` argv
sentinel, and the `__main__` block) — the actual GUI lives in the `launcher/` package:

- `launcher/app.py` — `CrawlerLauncher`, an explicit state machine (`AppState`: IDLE → STARTING → RUNNING →
  CHECKING → CANCELLING → DRAINING → STOPPING) driving an sv-ttk-themed UI. Server start/stop runs on a background
  daemon thread so Tkinter's mainloop stays responsive. Uvicorn is started programmatically (`uvicorn.Server`) so
  `should_exit = True` can be set for graceful shutdown.
- `launcher/tray.py` — `TrayController`, a `pystray` icon on its own thread. `pystray` auto-selects a backend per OS
  (win32 / darwin / appindicator→gtk→xorg on Linux) at import time, so this is safe cross-platform; Linux tray
  *visibility* still depends on the desktop having a systray host. Closing the window minimizes to tray; only the
  Stop Server button or the tray's Quit item run the real shutdown flow.
- `launcher/notifications.py` — thin `notifypy` wrapper for cross-platform toast notifications (server start/stop,
  job/campaign completion, crashes). Deliberately not `winotify`, which imports the Windows-only `winreg` stdlib
  module at load time and crashes the whole GUI on macOS/Linux.

Because the GUI's Tkinter thread and the Uvicorn/asyncio event loop run on different OS threads,
`CrawlerLauncher` never reaches into crawler/dispatcher task dicts directly (`asyncio.Task.cancel()` isn't
thread-safe to call cross-thread) — it talks to its own local server over plain HTTP instead, the same way the web
frontend does. Before stopping the server it polls `GET /api/system/activity`; if anything is active, it confirms
with the user, calls `POST /api/system/cancel-all`, and waits for everything to actually drain before shutting
Uvicorn down. See [`api-reference.md`](api-reference.md#system) for both endpoints.

### 2. Web Application — `portal/`

A FastAPI application served by Uvicorn (ASGI). Jinja2 templates render the browser UI. All data mutations happen
through REST API calls from vanilla JavaScript on the frontend. The application is created by `create_app(config, db)`
in `portal/api/server.py` and wired with a `lifespan` context manager that starts/stops the shared Playwright browser
instance.

### 3. Crawler Engine — `portal/crawler/engine.py`

Runs as an `asyncio.Task` on the shared Uvicorn event loop. Multiple worker coroutines consume from a shared
`asyncio.PriorityQueue`. Blocking work (HTML parsing, DB writes) is offloaded to two `ThreadPoolExecutor` pools to keep
the event loop free for I/O.

### 4. SMTP Dispatcher — `portal/api/dispatcher.py`

Also runs as an `asyncio.Task` on the shared event loop. A tight loop fetches the next QUEUED email from the DB and
sends it via `aiosmtplib`. Credential rotation and rate-limit cooldowns are managed in-loop.

### 5. Lead Scoring — `portal/services/lead_scoring.py`

Not a background task — a pure function, `compute_lead_score()`, called synchronously wherever a lead is written or
edited (`save_lead()`, `update_lead()`, manual CSV import). Produces a 0–100 `lead_score` from the email's
`confidence_band` plus the presence of `person_name`, `designation`, and `phone`, weighted by the `lead_score.weights`
config section (see [configuration.md](configuration.md)). Manual (CSV-imported) leads always score 0 by design —
the score exists to help prioritize *crawled* leads, not to grade manual entries. `Database._ensure_columns()` calls
`_recompute_lead_scores()` on every startup, so a weight change in config takes effect retroactively for every
existing lead, not just newly-crawled ones.

---

## Data Flow

### Crawl Job Lifecycle

```
User (UI)
  │  POST /api/jobs  {domain_ids: [...]}
  ▼
jobs.py — create_job() → CrawlJob (status=running)
  │  For each seed domain: create_crawl_snapshot(job_id, domain) — freezes that
  │  domain's metadata into crawl_snapshots; seeds carry the snapshot id, not
  │  the mutable domains.id (see database-schema.md#crawl_snapshots)
  │  asyncio.create_task(_run_crawl(job_id, seeds))
  ▼
CrawlerEngine.run(seeds)
  │  Enqueue seed URLs to PriorityQueue
  ▼
  Worker × N  (asyncio coroutines)
  │  GET url via httpx
  │  If JS indicators detected → fallback to Playwright
  │  ThreadPool: parse HTML (BeautifulSoup)
  │    └─ extract_leads() → [Lead, ...]
  │    └─ link discovery  → [url, ...]
  │  ThreadPool: DB write → save_lead(), mark_visited()
  │  Enqueue new links (filtered by max_links_per_page, max_depth)
  ▼
  queue.join()  (all items processed)
  │
  db.finish_job(status="done")

User polls  GET /api/jobs/{id}  every 3 s for metrics
```

### Campaign Dispatch Lifecycle

```
User (UI)
  │  POST /api/campaigns  {lead_ids, template_id, name, credential_ids?}
  ▼
campaigns.py
  │  Load leads → check blacklist → render Jinja2 template per lead
  │  bulk_create_campaign_emails() → CampaignEmail (status=DRAFT)
  │  set_campaign_credentials()  (optional; empty = any active credential)
  ▼
User reviews drafts, edits, deselects; may PUT .../credentials to change the pool
  │  POST /api/campaigns/{id}/dispatch
  ▼
dispatcher.py — run_campaign_dispatch(campaign_id, db)
  │  queue_campaign_emails()  DRAFT(selected) → QUEUED
  │  Loop:
  │    Check campaign status (PAUSED/CANCELLED → break)
  │    get_next_queued_email()
  │    resolve_credential_pool()  (this campaign's assigned credentials, else
  │      all active; excludes any credential over its daily_send_limit)
  │    _get_next_credential()  (round-robin over that pool, re-read every iteration)
  │    _wait_for_credential_slot()  — 30-90s gap per CREDENTIAL id (not per loop
  │      iteration), shared across every campaign in the process
  │    aiosmtplib: send email
  │    On success            → mark_email_sent()
  │    On hard bounce (550/553, incl. SMTPRecipientsRefused) → add_to_blacklist() + mark_email_failed()
  │    On rate limit (421/450/451)  → set_credential_cooldown(+1h), retry
  │    On auth failure        → disable_credential(), retry (email not marked failed)
  │    On network error       → set_credential_cooldown(+15min), retry
  │    No usable credential   → campaign PAUSED with pause_reason set
  ▼
campaign status → COMPLETED / PAUSED (pause_reason set if auto-paused)
```

### Domain Import Lifecycle

```
User (UI or CLI)
  │  POST /api/import/json  (file upload)
  │  POST /api/import        (live API)
  ▼
imports.py — asyncio.create_task(_run_json_import / _run_import)
  ▼
  asyncio.to_thread(import_from_json / import_all)  ← blocking, runs off event loop
  │
  JSON mode:
  │  Parse gov_domains.json → {category → state → org_type → [url_or_entry_obj]}
  │  db.clear_domains()
  │  db.upsert_domain() × N  (external_id when the entry carries one, else main_url)
  │
  API mode:
  │  GovScraper.get_categories()
  │  For each category:
  │    GovScraper.get_entries_for_category()
  │    db.upsert_domain() × N  (external_id = npi_sanitized_id)
  │
  Both modes: entries with no crawlable URL are kept with main_url=None
  instead of being dropped — the frontend marks them "not crawlable" and
  lets a user add a URL later via PATCH /api/domains/{id}.
  │
  import_status dict updated in-place (polled by /api/import/status)
```

---

## Async / Threading Model

```
Thread: Uvicorn main (event loop)
  ├── asyncio coroutines (FastAPI handlers, CrawlerEngine workers, SMTP dispatcher)
  └── Executor calls → thread pool

Thread pool: db_pool (1 thread, serialized writes)
  └── db.save_lead(), db.mark_visited(), db.update_job_metrics()

Thread pool: parse_pool (cpu_count threads)
  └── parser.parse_for_engine() — BeautifulSoup, extract_leads()

Thread pool: asyncio.to_thread (import)
  └── import_from_json() / import_all()

Thread: Tkinter main loop (only in GUI mode, launcher/app.py)
  ├── trigger_start_server() → starts Uvicorn on its own daemon thread
  ├── background threads per HTTP call → httpx.Client, results marshaled
  │   back via root.after(0, ...)
  └── TrayController.start() → pystray icon on its own daemon thread
      (callbacks marshal back to the Tkinter thread the same way)
```

The DB write pool uses exactly **1 thread** to serialize writes, avoiding SQLite WAL contention. The parse pool uses all
available CPU cores because BeautifulSoup is GIL-bound but benefits from OS-level parallelism when multiple HTML strings
are parsed concurrently.

---

## Component Boundaries

| Component    | Entry Point                                                    | State it Owns                                                                                                                                                      |
|--------------|----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| GUI          | `launcher/app.py:CrawlerLauncher`                              | `AppState`, Uvicorn server handle, thread references, `httpx.Client`                                                                                               |
| Tray/Toast   | `launcher/tray.py:TrayController`, `launcher/notifications.py` | pystray icon + thread; stateless notification calls                                                                                                                |
| Web App      | `portal/api/server.py:create_app`                              | `portal/api/deps.py` (`_db`, `_config`, `_browser`, `_active_tasks`)                                                                                               |
| Crawler      | `portal/crawler/engine.py:CrawlerEngine`                       | `_queue`, `_visited`, `_domain_locks`                                                                                                                              |
| Dispatcher   | `portal/api/dispatcher.py:run_campaign_dispatch`               | Module-level `_credential_locks`/`_credential_last_sent` (per-credential send pacing, shared across all campaigns in the process); everything else persisted in DB |
| System       | `portal/api/system.py`                                         | No state of its own — aggregates `_active_tasks` + `campaigns._active_campaign_tasks` + DB status for the GUI                                                      |
| Importer     | `portal/scraper/importer.py`                                   | `import_status` module-level dict                                                                                                                                  |
| Lead Scoring | `portal/services/lead_scoring.py:compute_lead_score`           | No state — pure function; weights passed in from config                                                                                                            |
| DB           | `portal/db/database.py:Database`                               | SQLAlchemy engine + session factory                                                                                                                                |

---

## Key Design Decisions

| Decision                                                        | Rationale                                                                                                                                                                                                                                                                      |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| HTTPX-first, Playwright fallback                                | ~60–70% of `.gov.in` sites are static HTML; skipping Playwright for those cuts crawl time significantly                                                                                                                                                                        |
| `asyncio.PriorityQueue`                                         | Contact/directory pages carry explicit priority keywords (e.g., `contact`, `officer`); these should be crawled before generic pages                                                                                                                                            |
| One shared browser, per-worker context                          | A single Playwright browser process shared across workers saves RAM; per-worker context isolates sessions and prevents `TargetClosedError` across workers                                                                                                                      |
| Single-thread DB pool                                           | SQLite WAL mode allows one writer + many readers. One serialized writer thread avoids write contention without transactions                                                                                                                                                    |
| Per-domain lock + delay                                         | Politeness spacing prevents hammering a single server while allowing workers serving other domains to proceed freely                                                                                                                                                           |
| Recrawl protection                                              | URLs visited within `recrawl_days` are pre-populated into `_visited` before crawl starts, advancing the frontier on re-runs without re-crawling fresh pages                                                                                                                    |
| Seed domains bypass recrawl                                     | Seeds are always re-crawled entry points; their child pages inherit recrawl protection so reruns pick up only new links                                                                                                                                                        |
| GUI talks to its own server over HTTP                           | The Tkinter thread and the Uvicorn/asyncio event loop are different OS threads; `asyncio.Task.cancel()` isn't thread-safe cross-thread, so the launcher polls `GET /api/system/activity` / calls `POST /api/system/cancel-all` instead of touching task dicts directly         |
| Confirm-then-drain shutdown                                     | Stopping the server while jobs/campaigns are active first confirms with the user, then cancels everything and polls until it actually stops (up to a 3-minute timeout with a force-stop escape hatch) before killing Uvicorn — avoids silently orphaning in-flight email sends |
| One elected pagination link per page, shared chain budget       | A numbered pager bar ("1 2 3 ... Next") would otherwise spawn N independent chains; electing exactly one link and sharing one `max_chain_children` budget across the whole chain bounds the amplification regardless of how many pages exist                                   |
| Pagination `param_signals` must resolve to a plain integer      | Session-URL "next page" links dressed up with a non-numeric/base64 param (seen in the wild on some `.gov.in` sites) would otherwise be followed as if they were real pagination — rejecting outright on a non-numeric matched param fails closed instead of open               |
| Per-credential send pacing, not per-loop-iteration              | A flat sleep between every send would waste time when multiple credentials are in rotation; pacing per credential id (shared across all campaigns) lets different credentials send back-to-back while still rate-limiting repeated use of the same one                         |
| Custom-URL jobs bypass `target_suffixes`                        | A caller supplying explicit URLs has already chosen them deliberately — restricting to `.gov.in`/`.nic.in` would silently defeat the point of the feature                                                                                                                      |
| Leads point at a per-crawl `crawl_snapshots` row, not `domains` | Domain imports/refreshes destructively rebuild `domains` (reassigning `domains.id`), which used to silently corrupt lead-visible metadata after a refresh; freezing each seed's metadata at crawl time decouples leads entirely from later catalog churn                       |
