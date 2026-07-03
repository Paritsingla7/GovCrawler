# GovCrawler

## Overview

GovCrawler is a full-stack platform for discovering, crawling, and extracting contact data from Indian Government
domains (`.gov.in`, `.nic.in`). It combines a direct API scraper (`GovScraper`) for domain discovery with an async
Playwright + HTTPX crawler for deep extraction (emails, personnel, designations). A FastAPI portal manages all jobs,
leads, and an email outreach system backed by SQLite or PostgreSQL.

## Key Features

- **Centralized Portal:** FastAPI web application (`portal/`) with a browser-based UI for all workflows.
- **Domain Discovery:** `GovScraper` extracts domains from the `india.gov.in` Web Directory API — no CAPTCHA needed.
  Organizations with no listed URL are kept (not dropped) and marked "not crawlable" until a URL is added manually.
- **Deep Crawler Engine:** Async crawling via HTTPX (fast path) with Playwright fallback for JavaScript-heavy sites.
- **Data Extraction:** Configurable regex/keyword extraction for emails and key personnel (name, designation,
  department). Phone number extraction is planned but not yet implemented.
- **Email Outreach System:** Full campaign lifecycle — Jinja2 email templates, lead-to-draft generation, SMTP dispatch
  with rate-limit handling, blacklist, and test campaigns.
- **Scalable Database:** SQLAlchemy ORM with SQLite (default) or PostgreSQL. Schema managed via Alembic migrations.

## Project Structure

```
GovCrawler/
├── run.py                   # Tkinter GUI Control Panel entry point
├── requirements.txt
├── GovCrawler.spec          # PyInstaller build configuration
├── alembic.ini              # Alembic database migration config
├── alembic/versions/        # Incremental schema migration scripts
├── GovScraper/              # Standalone domain-discovery tool (india.gov.in API); has
│                            # its own CLI to generate gov_domains.json — see its README
└── portal/                  # Core FastAPI application package
    ├── main.py              # CLI dispatcher and server factory
    ├── paths.py             # Path resolution + first-run bootstrap (dev + PyInstaller)
    ├── default_config.yaml  # Shipped default configuration
    ├── config.yaml          # Live user configuration (gitignored)
    ├── api/                 # REST API layer — one APIRouter per concern
    │   ├── server.py        # App factory: lifespan, static mount, include_router × 10
    │   ├── deps.py          # Shared app state (db/config/browser) + Depends() providers
    │   ├── frontend.py      # HTML page routes + /api/logs, /api/visited-urls
    │   ├── domains.py       # Domain metadata, browsing, stats, and URL-edit routes
    │   ├── config.py        # Crawler/extraction settings routes
    │   ├── imports.py       # Domain import routes + background tasks
    │   ├── jobs.py          # Crawl job routes + background crawl task
    │   ├── leads.py         # Lead browsing, export, and editing routes
    │   ├── campaigns.py     # Campaign generation + dispatch routes
    │   ├── dispatcher.py    # Async SMTP background worker
    │   ├── credentials.py   # SMTP credential CRUD
    │   ├── templates.py     # Email template CRUD (Jinja2 validated)
    │   └── blacklist.py     # Email/domain blacklist CRUD
    ├── services/
    │   └── campaign_service.py  # Draft rendering shared by campaign create/add-emails
    ├── crawler/
    │   ├── engine.py        # CrawlerEngine: priority queue, httpx-first, Playwright fallback
    │   └── parser.py        # Email + personnel extraction + parse_for_engine entry point
    ├── db/
    │   ├── base.py          # declarative_base() + SQLite WAL pragma
    │   ├── enums.py         # CampaignStatus, EmailStatus
    │   ├── tables/          # ORM models (crawl, leads, outreach)
    │   ├── mixins/          # Database's methods, grouped by concern
    │   └── database.py      # Database class, composed from the mixins
    ├── scraper/
    │   └── importer.py      # JSON and live-API domain import handlers
    ├── frontend/            # Jinja2 HTML templates + vanilla JS/CSS
    └── data/                # Runtime data (SQLite DB, log file)
```

See [`.docs/directory-structure.md`](.docs/directory-structure.md) for a full annotated file tree.

## Quick Start (Pre-compiled Release)

1. Download the latest `GovCrawler-vX.Y.Z-windows.zip` from the Releases page.
2. Extract the `.zip` file.
3. Double-click **`GovCrawler.exe`** to open the Control Panel GUI.

## Prerequisites (Source Installation)

- Python 3.10+
- Playwright Chromium browser (`playwright install chromium`)

## Installation from Source

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd GovCrawler
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # Linux/Mac
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers:**
   ```bash
   playwright install chromium
   ```

## Usage

### Graphical User Interface (GUI)

```bash
python run.py
```

Opens the **GovCrawler Control Panel** (`CrawlerLauncher`) with four buttons:

1. **Download Browsers** — First-time setup; downloads ~600 MB Chromium.
2. **Start Server** — Launches the FastAPI server on `http://127.0.0.1:8000`.
3. **Open Web Interface** — Opens the browser UI.
4. **Stop Server** — Gracefully shuts down Uvicorn.

### CLI Usage

| Command                               | Description                                      |
|---------------------------------------|--------------------------------------------------|
| `python -m portal`                    | Start the server (default, same as `serve`)      |
| `python -m portal serve`              | Start the server explicitly                      |
| `python -m portal import-json [path]` | Seed DB from `gov_domains.json` (zero API calls) |
| `python -m portal import`             | Refresh domains from the live `india.gov.in` API |
| `python -m portal crawl <job_id>`     | Manually trigger a specific crawl job            |

## Workflow Overview

### 1. Seed the Database

Import Indian government domains in one of two ways:

- **JSON import (recommended):** Upload `gov_domains.json` via the Settings page or CLI. Zero API calls, instant.
  Generate the file standalone with GovScraper's own CLI (see [`GovScraper/README.md`](GovScraper/README.md)):
  ```bash
  cd GovScraper && python runner.py ../gov_domains.json
  ```
  then import it:
  ```bash
  python -m portal import-json gov_domains.json
  ```
- **Live API import:** Fetches fresh data directly from `india.gov.in`. Use only when refreshing.
  ```bash
  python -m portal import
  ```

Organizations with no listed URL are imported anyway (metadata preserved, `main_url: null`) instead of being
dropped — the domain browser marks them "not crawlable" and lets you add a URL later.

### 2. Create a Crawl Job

From the web UI: filter domains by category/state/org-type, select them, and click **Start Crawl Job**. The engine
crawls all selected domains up to `max_depth`, extracting emails and personnel.

### 3. Review Leads

Navigate to `/leads`. Filter by category, state, job, or search term. Edit person names, designations, or departments
inline. Export to CSV.

### 4. Email Outreach

1. Create an **Email Template** with Jinja2 variables (e.g., `{{ name }}`, `{{ designation }}`).
2. Create a **Campaign** by selecting leads and a template. Drafts are auto-rendered.
3. Review, edit, or deselect individual draft emails.
4. Add **SMTP Credentials** (host, port, username, password).
5. **Dispatch** the campaign. The background worker sends emails with per-credential rate-limit handling.

## Building an Executable

```bash
pip install pyinstaller
pyinstaller GovCrawler.spec
```

Output: `dist/GovCrawler/GovCrawler.exe`

## Configuration

Settings live in `portal/config.yaml`. The application ships with `portal/default_config.yaml` as a template. Key
sections:

| Section                                  | Key Settings                                         |
|------------------------------------------|------------------------------------------------------|
| `database.uri`                           | SQLite (default) or `postgresql://user:pass@host/db` |
| `api.host` / `api.port`                  | Server bind address (default `0.0.0.0:8000`)         |
| `crawler.workers`                        | Concurrent async workers (default 50)                |
| `crawler.max_depth`                      | Max crawl depth per seed (default 3)                 |
| `crawler.recrawl_days`                   | Skip URLs visited within N days (default 30)         |
| `extraction.email.valid_suffixes`        | Only keep emails matching these domains              |
| `extraction.person.designation_keywords` | Keywords that trigger designation detection          |

See [`.docs/configuration.md`](.docs/configuration.md) for the full reference.

---

## Team Workflow & Collaboration Guidelines

### 1. Branching Strategy

- `main` — Stable, production-ready.
- `develop` — Integration branch.
- Feature branches: `feature/<issue-number>-<brief-desc>`
- Bugfix branches: `bugfix/<issue-number>-<brief-desc>`

### 2. Development Workflow

1. Pull the latest `develop` before starting work.
2. Create your feature branch from `develop`.
3. Commit often with clear messages (e.g., `feat(crawler): add depth tracking to job metrics`).
4. Open a PR against `develop`.

### 3. Code Review & Pull Requests

- All PRs require at least **one reviewer** before merging.
- Ensure code is locally tested and all relevant tests pass.
- Provide a clear description: what it fixes, how it was tested, any side effects.
- Do not merge your own PRs without approval.

### 4. Coding Standards

- **Style:** Follow [PEP 8](https://peps.python.org/pep-0008/). Use `black` for formatting and `ruff` for linting.
- **Type Hints:** Required on all functions and class members.
- **Docstrings:** Write module/class/function docstrings explaining *why*, not just *what*.
- **Logging:** Use the `logging` module exclusively (`log.info()`, `log.error()`). Never use `print()`.

### 5. Database Migrations

Changes to ORM models in `portal/db/tables/` must be accompanied by an Alembic migration script in
`alembic/versions/`. For backward-compatible column additions, `_ensure_columns()` in `Database.__init__`
(`portal/db/database.py`) can be used as a lightweight alternative during development. Communicate schema changes to
the team before merging.

### 6. Async Patterns

- Never call blocking I/O directly inside `async` functions that share the Uvicorn event loop.
- Use `asyncio.to_thread()` or a `ThreadPoolExecutor` for synchronous DB calls and CPU-bound parsing.
- Each worker has a dedicated Playwright browser context to prevent `TargetClosedError` across workers.
- Handle timeouts and network errors so one page failure never halts the entire crawl.

---

## Further Documentation

| Doc                                                            | Description                       |
|----------------------------------------------------------------|-----------------------------------|
| [`.docs/architecture.md`](.docs/architecture.md)               | System architecture and data flow |
| [`.docs/directory-structure.md`](.docs/directory-structure.md) | Annotated file tree               |
| [`.docs/api-reference.md`](.docs/api-reference.md)             | All REST endpoints                |
| [`.docs/database-schema.md`](.docs/database-schema.md)         | ORM models and column reference   |
| [`.docs/crawler.md`](.docs/crawler.md)                         | Crawler engine internals          |
| [`.docs/outreach.md`](.docs/outreach.md)                       | Email outreach system             |
| [`.docs/configuration.md`](.docs/configuration.md)             | Full config reference             |
