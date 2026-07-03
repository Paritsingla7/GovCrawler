# Architecture

## High-Level Overview

GovCrawler is a monolithic Python application composed of four logically distinct subsystems that share a single FastAPI
process and SQLite/PostgreSQL database.

```
┌──────────────────────────────────────────────────────────┐
│                    GovCrawler Process                    │
│                                                          │
│  ┌─────────────┐   ┌─────────────────────────────────┐  │
│  │  Tkinter GUI │   │        FastAPI / Uvicorn         │  │
│  │  (run.py)   │──▶│  (portal/api/server.py)          │  │
│  └─────────────┘   │                                  │  │
│                    │  Frontend routes  API routes      │  │
│                    │  /  /leads        /api/domains    │  │
│                    │  /campaigns       /api/jobs       │  │
│                    │  /settings        /api/leads      │  │
│                    │                   /api/campaigns  │  │
│                    └──────────┬──────────────┬─────────┘  │
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

### 1. Control Plane — `run.py` (Tkinter GUI)

The entry point for end users running the Windows executable. Tkinter renders a four-button control panel in the main OS
thread. Server start/stop is managed on a background daemon thread so the GUI stays responsive. Uvicorn is started
programmatically (`uvicorn.Server`) so `should_exit = True` can be set for graceful shutdown.

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

---

## Data Flow

### Crawl Job Lifecycle

```
User (UI)
  │  POST /api/jobs  {domain_ids: [...]}
  ▼
jobs.py — create_job() → CrawlJob (status=running)
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
  │  POST /api/campaigns  {lead_ids, template_id, name}
  ▼
campaigns.py
  │  Load leads → check blacklist → render Jinja2 template per lead
  │  bulk_create_campaign_emails() → CampaignEmail (status=DRAFT)
  ▼
User reviews drafts, edits, deselects
  │  POST /api/campaigns/{id}/dispatch
  ▼
dispatcher.py — run_campaign_dispatch(campaign_id, db)
  │  queue_campaign_emails()  DRAFT(selected) → QUEUED
  │  Loop:
  │    Check campaign status (PAUSED/CANCELLED → break)
  │    get_next_queued_email()
  │    _get_next_credential()  (round-robin)
  │    aiosmtplib: send email
  │    mark_email_sent() / mark_email_failed()
  │    On hard bounce (550/553) → add_to_blacklist()
  │    On rate limit (421/450/451) → set_credential_cooldown()
  │    await asyncio.sleep(random 30–90 s)
  ▼
campaign status → COMPLETED / PAUSED
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

Thread: Tkinter main loop (only in GUI mode, run.py)
  └── trigger_start_server() → starts server on daemon thread
```

The DB write pool uses exactly **1 thread** to serialize writes, avoiding SQLite WAL contention. The parse pool uses all
available CPU cores because BeautifulSoup is GIL-bound but benefits from OS-level parallelism when multiple HTML strings
are parsed concurrently.

---

## Component Boundaries

| Component  | Entry Point                                      | State it Owns                                 |
|------------|--------------------------------------------------|-----------------------------------------------|
| GUI        | `run.py:CrawlerLauncher`                         | Uvicorn server handle, thread references      |
| Web App    | `portal/api/server.py:create_app`                | `portal/api/deps.py` (`_db`, `_config`, `_browser`, `_active_tasks`) |
| Crawler    | `portal/crawler/engine.py:CrawlerEngine`         | `_queue`, `_visited`, `_domain_locks`         |
| Dispatcher | `portal/api/dispatcher.py:run_campaign_dispatch` | In-loop locals only; state persisted in DB    |
| Importer   | `portal/scraper/importer.py`                     | `import_status` module-level dict             |
| DB         | `portal/db/database.py:Database`                 | SQLAlchemy engine + session factory           |

---

## Key Design Decisions

| Decision                               | Rationale                                                                                                                                                   |
|----------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| HTTPX-first, Playwright fallback       | ~60–70% of `.gov.in` sites are static HTML; skipping Playwright for those cuts crawl time significantly                                                     |
| `asyncio.PriorityQueue`                | Contact/directory pages carry explicit priority keywords (e.g., `contact`, `officer`); these should be crawled before generic pages                         |
| One shared browser, per-worker context | A single Playwright browser process shared across workers saves RAM; per-worker context isolates sessions and prevents `TargetClosedError` across workers   |
| Single-thread DB pool                  | SQLite WAL mode allows one writer + many readers. One serialized writer thread avoids write contention without transactions                                 |
| Per-domain lock + delay                | Politeness spacing prevents hammering a single server while allowing workers serving other domains to proceed freely                                        |
| Recrawl protection                     | URLs visited within `recrawl_days` are pre-populated into `_visited` before crawl starts, advancing the frontier on re-runs without re-crawling fresh pages |
| Seed domains bypass recrawl            | Seeds are always re-crawled entry points; their child pages inherit recrawl protection so reruns pick up only new links                                     |
