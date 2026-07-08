# GovCrawler

![CI](https://github.com/Jaguar000212/GovCrawler/actions/workflows/ci.yaml/badge.svg)

A multi-user platform for discovering, crawling, and extracting contact data from Indian government domains
(`.gov.in`, `.nic.in`), with an email-outreach system on top of the harvested leads. It pairs a direct API
scraper (`GovScraper`) for domain discovery with an async HTTPX + Playwright crawler for deep extraction
(emails, personnel, designations), all behind an authenticated FastAPI application with role-based access
control.

## Architecture at a glance

The code is split into three tiers plus a thin entry-point shim:

| Tier | Package | Runs where |
|------|---------|-----------|
| **Shared** | `shared/` | enums, permission catalog, wire DTOs, lead-scoring — imported by both tiers |
| **Cloud** | `cloud/` | the VPS: FastAPI app, auth/RBAC, Postgres database of record, SMTP dispatcher, admin UI |
| **Agent** | `agent/` | each machine: the crawler engine + parser, durable local outbox/frontier, Tkinter launcher |
| **Shim** | `portal/` | `load_config()`, path/bootstrap, and the `python -m portal` CLI |

There is one trust boundary — the cloud API. Postgres is never exposed; clients reach shared data only
through authenticated, RBAC-checked HTTP. See [`.docs/architecture.md`](.docs/architecture.md).

## Key features

- **Auth & RBAC** — argon2id passwords, short-lived JWTs with rotating refresh tokens, per-user permission
  overrides, append-only audit log. See [`.docs/authentication.md`](.docs/authentication.md).
- **Domain discovery** — `GovScraper` reads the `india.gov.in` Web Directory API (no browser, no CAPTCHA).
  Organizations with no listed URL are kept and marked "not crawlable" rather than dropped.
- **Deep crawler** — async HTTPX (fast path) with Playwright fallback for JavaScript-heavy sites; priority
  queue, per-domain politeness, pagination-aware, recrawl protection.
- **Extraction & scoring** — a 6-stage, confidence-scored pipeline (mailto/tel, microdata, tables,
  proximity text) producing 0–100 lead scores from configurable weights.
- **Shared lead pool** — global email dedup with enrich-on-conflict and per-job attribution
  (`lead_occurrences`).
- **Email outreach** — Jinja2 templates, lead-to-draft generation, centralized SMTP dispatch with
  per-credential pacing, hard-bounce blacklisting, and at-most-once delivery.
- **Resilience** — a durable local outbox (no lead lost on outage/crash), frontier checkpoint + exact
  resume, stale-job reaping, and dispatch recovery. See [`.docs/resilience.md`](.docs/resilience.md).

## Two ways to run

### 1. Desktop launcher (single operator)

A Tkinter control panel, shipped as a signed-per-OS `.exe`/app for Windows, macOS, and Linux.

- **Pre-compiled:** download the latest `GovCrawler-vX.Y.Z-<os>.zip` from the Releases page, extract, and
  run `GovCrawler` — it opens the Control Panel.
- **From source:**
  ```bash
  git clone <repo-url> && cd GovCrawler
  python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
  pip install -r requirements.txt
  playwright install chromium
  python run.py
  ```

The launcher installs Chromium on first run (one click, ~600 MB), then starts the server on
`http://127.0.0.1:8001`, prompts for login, and polls activity. Closing the window minimizes to the tray;
Stop Server runs a confirm → cancel → drain shutdown. Desktop toasts fire on job/campaign completion.

### 2. Cloud stack (multi-user, VPS)

```bash
cd deploy
cp .env.example .env          # fill in POSTGRES_PASSWORD, JWT_SECRET, CREDENTIAL_ENC_KEY, DOMAIN, ...
docker compose up --build -d
docker compose exec api python -m portal create-admin you@example.com
```

Services: `db` (Postgres + WAL archiving), `migrate` (one-shot Alembic), `api` (FastAPI + admin dashboard at
`/admin/dashboard`), `dispatcher` (standalone SMTP loop), `proxy` (Caddy, automatic TLS). Full details in
[`.docs/deployment.md`](.docs/deployment.md) and the `deploy/` runbooks
([SECURITY.md](deploy/SECURITY.md), [BACKUP.md](deploy/BACKUP.md), [PITR.md](deploy/PITR.md)).

## CLI (`python -m portal`)

| Command | Description |
|---------|-------------|
| `python -m portal` / `serve` | Start the server |
| `python -m portal import-json [path]` | Seed the catalog from `gov_domains.json` (zero API calls) |
| `python -m portal import` | Refresh domains from the live `india.gov.in` API |
| `python -m portal crawl <job_id>` | Re-run a crawl job synchronously (debug) |
| `python -m portal create-admin <email> [password]` | Provision the first admin user |

## Workflow

1. **Seed the catalog.** Generate `gov_domains.json` standalone (`cd GovScraper && python runner.py
   ../gov_domains.json`), then `python -m portal import-json gov_domains.json` — or use the live API import.
2. **Create a crawl job.** Filter domains by category/state/org-type, select them, and start a job (or supply
   custom URLs). The engine crawls to `max_depth`, extracting leads through the durable outbox.
3. **Review leads.** Browse `/leads` — filter, search, edit names/designations/departments, export CSV.
4. **Run a campaign.** Create a Jinja2 template, generate drafts from selected leads, review/deselect, assign
   SMTP credentials, and dispatch. The dispatcher paces sends per credential and auto-blacklists hard bounces.

## Building an executable

```bash
pip install pyinstaller
pyinstaller GovCrawler.spec        # output: dist/GovCrawler/GovCrawler(.exe)
```

`GovCrawler.spec` bundles `cloud/frontend`, `alembic`, `assets/favicon.ico`, and the default config. The
tag-triggered `release.yaml` workflow builds this for Windows/macOS/Linux.

## Development

- **Style:** PEP 8; `black` and `ruff` (config in `pyproject.toml`, line length 120). Type hints on all
  functions. Logging only — never `print()` (CLI usage output excepted).
- **Tests:** `pip install -r requirements-dev.txt && pytest -q`.
- **Migrations:** ORM changes in `cloud/db/tables/` need an Alembic revision (`alembic revision
  --autogenerate -m "..."`). `_ensure_columns()` is only for lightweight additive dev-time changes.
- **CI** (`.github/workflows/ci.yaml`): diff-scoped lint, import-sanity, `pytest`, and a Postgres migration
  smoke test.
- **Branching:** feature/bugfix branches → PR into `develop`; `main` is stable/production.
- **Async:** never block the event loop — use `asyncio.to_thread()` or the crawler's thread pools.

## Documentation (`.docs/`)

| Doc | Covers |
|-----|--------|
| [architecture.md](.docs/architecture.md) | Tiers, trust model, data flow, threading |
| [directory-structure.md](.docs/directory-structure.md) | Annotated file tree |
| [authentication.md](.docs/authentication.md) | Auth, JWT/refresh, RBAC, permissions & roles |
| [api-reference.md](.docs/api-reference.md) | Every REST endpoint + its permission guard |
| [database-schema.md](.docs/database-schema.md) | Cloud schema, local store, migrations |
| [crawler.md](.docs/crawler.md) | Crawler engine + 6-stage extraction pipeline |
| [outreach.md](.docs/outreach.md) | Campaigns, dispatcher, credentials, blacklist |
| [resilience.md](.docs/resilience.md) | Outbox, resume, reaping, dispatch recovery, DR |
| [configuration.md](.docs/configuration.md) | Full config + env-var reference |
| [deployment.md](.docs/deployment.md) | Docker/VPS stack, secrets, backups |
