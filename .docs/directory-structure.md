# Directory Structure

Annotated file tree. Runtime artefacts (`__pycache__/`, `playwright_browsers/`, `portal/data/`,
`deploy/.env`, `deploy/wal_archive/`, `deploy/backups/`) and IDE folders are omitted.

```
GovCrawler/
│
├── run.py                     # Desktop entry point (PyInstaller target): SSL cert fix,
│                              # "INSTALL_BROWSERS" argv sentinel, no-console stdio guard,
│                              # then launches agent.launcher.app.CrawlerLauncher
├── GovCrawler.spec            # PyInstaller spec — bundles frontend, assets, config (no alembic — the
│                              #   desktop agent never runs Alembic migrations)
├── alembic.ini                # Alembic config; env.py targets cloud.db.Base (cloud/VPS only)
├── pyproject.toml             # ruff + black (line-length 120, py311) + pytest + import-linter config
├── requirements.txt           # Dev shim: -r requirements/cloud.txt + -r requirements/agent.txt (the VPS
│                              #   Docker image installs requirements/cloud.txt directly, not this shim)
├── requirements/               # Per-tier pins — shared.txt, cloud.txt (+shared), agent.txt (+shared, incl. jinja2)
├── requirements-dev.txt       # -r requirements.txt + pytest/ruff/black/import-linter
├── README.md
│
├── shared/                    # Framework-light; imported by BOTH cloud and agent, imports neither
│   ├── enums.py               # CampaignStatus, CampaignKind, EmailStatus, JobStatus
│   ├── permissions.py         # PERMISSIONS catalog (19 keys) + ROLE_DEFAULTS + BUILTIN_ROLES
│   ├── scoring.py             # compute_lead_score(), DEFAULT_WEIGHTS (pure function)
│   └── schemas/
│       └── auth.py            # Pydantic DTOs: LoginRequest, RefreshRequest, UserOut, TokenResponse
│
├── cloud/                     # THE VPS APP — FastAPI + Postgres, auth, admin, dispatcher. Genuinely
│   │                          # crawler-free: no agent.* imports, no Playwright (plan.md §19.1 Phase 9 Part 2)
│   ├── api/
│   │   ├── server.py          # create_app(config, db): routers, lifespan (reaper only — no browser),
│   │   │                      #   CORS/CSRF, static mount, JWT-secret bootstrap
│   │   ├── deps.py            # get_current_user, require(), require_loopback, verify_csrf, client_ip,
│   │   │                      #   CurrentUser, RedirectException, shared app state
│   │   ├── auth.py            # /auth/login|refresh|logout|me (no /auth/bootstrap — retired, see agent/bff)
│   │   ├── admin.py           # /api/admin/users|roles|permissions + permission-override PUT (users.manage)
│   │   ├── audit.py           # GET /api/admin/audit (audit.view — deliberately separate from users.manage)
│   │   ├── coordination.py    # /api/coordination/* — the agent↔cloud contract; resume enforces agent_id match
│   │   ├── frontend.py        # HTML page routes (Jinja2) — admin-only (login/admin-dashboard/admin-guide) +
│   │   │                      #   /api/logs (cloud's own server log)
│   │   ├── system.py          # /healthz, /api/admin/activity, /api/admin/system-status (DB-backed)
│   │   ├── config.py          # GET/POST /api/config — the crawl-policy "settings" router
│   │   ├── domains.py         # catalog browse + PATCH a no-URL domain's URL
│   │   ├── imports.py         # /api/import/json|/api/import|/api/import/status (single-flight)
│   │   ├── jobs.py            # read-only job list/detail/seeds (creation lives in agent/api.py)
│   │   ├── leads.py           # shared-pool browse/export/import-csv/edit
│   │   ├── campaigns.py       # campaign + email staging + dispatch routes
│   │   ├── dispatcher.py      # run_campaign_dispatch() SMTP loop (shared by both modes)
│   │   ├── credentials.py     # SMTP credential CRUD + live connection test
│   │   ├── templates.py       # Jinja2 email-template CRUD (validated)
│   │   └── blacklist.py       # email/domain blacklist CRUD
│   ├── db/
│   │   ├── base.py            # declarative_base() + SQLite WAL pragma listener
│   │   ├── database.py        # Database class, composed from 7 mixins; _ensure_columns()
│   │   ├── enums.py           # re-export of shared.enums (import compat)
│   │   ├── migrations.py      # run_migrations(): stamp-then-upgrade on startup
│   │   ├── tables/            # auth.py, crawl.py, leads.py, lookups.py, outreach.py, settings.py
│   │   └── mixins/            # auth (+ permission overrides + audit list), domain, job (+ agent-ownership
│   │                          #   guard), crawl_snapshot, lead, outreach, app_settings — no visited_mixin
│   ├── security/
│   │   ├── hashing.py         # argon2id hash/verify/needs_rehash
│   │   ├── jwt.py             # HS256 access tokens + opaque refresh tokens
│   │   └── crypto.py          # Fernet credential encryption + key rotation (MultiFernet)
│   ├── services/
│   │   ├── campaign_service.py # render_draft_emails() — blacklist filter + Jinja2 + missing-field detect
│   │   ├── csv_import.py      # parse_contacts_csv(), build_template_csv()
│   │   └── importer.py        # import_from_json() / import_all() into the domains catalog (india.gov.in
│   │                          #   Web Directory API calls inlined here — see GovScraper/ below)
│   └── dispatch_service.py    # `python -m cloud.dispatch_service` — standalone (external) dispatcher
│
├── frontend/                   # Three clearly-separated trees — no template/asset is ambiguous about which
│   │                          # tier renders it (UI overhaul, see .docs/architecture.md#frontend).
│   ├── shared/                 # Tier-agnostic: login page, design tokens, generic components, the shared
│   │   ├── templates/          #   error/toast JS. Loaded by BOTH cloud and agent apps.
│   │   │   └── login.html      # standalone (doesn't extend either tier's base.html); identical on both
│   │   └── static/
│   │       ├── css/            # tokens.css (CSS vars) + components.css (buttons/tables/modals/badges/…)
│   │       ├── js/             # http.js (apiFetch/ApiError/friendlyMessage + CSRF patch), toast.js
│   │       │                   #   (showToast/showApiError), login.js
│   │       └── img/favicon.ico
│   ├── agent/                   # The crawler+outreach UI — rendered only by agent/bff/pages.py
│   │   ├── templates/
│   │   │   ├── base.html        # layout + nav; the Admin nav button is an external-only link-out
│   │   │   │                    #   ("Admin Portal ↗") to the cloud's own login, never rendered UI
│   │   │   ├── index.html       # domains browser + crawl job creation + live status
│   │   │   ├── leads.html
│   │   │   ├── campaigns.html
│   │   │   ├── settings.html    # crawler policy + outreach (SMTP/templates/blacklist) config
│   │   │   ├── test-campaign.html
│   │   │   └── user-guide.html
│   │   └── static/
│   │       ├── css/             # agent.css (dock/sidebar/config-drawer chrome) + leads/campaigns/settings.css
│   │       └── js/              # base.js, leads.js, campaigns.js, settings.js, test-campaign.js
│   └── cloud/                    # Rendered by cloud/api/frontend.py. ⚠️ NOT admin-only (issue #58):
│       ├── templates/            #   also ships leads/campaigns pages that duplicate the agent tree
│       │   ├── base.html         # layout + nav (Admin Dashboard / Admin Guide / Logout)
│       │   ├── admin-dashboard.html  # /admin/dashboard + / (require jobs.view_all) — sidebar-tab page:
│       │   │                    #   Overview / Users / Roles (read-only) / Audit Log / System (health card)
│       │   ├── admin-guide.html  # short admin-only workflow doc
│       │   ├── leads.html        # ⚠️ #58: cloud twin of frontend/agent/templates/leads.html
│       │   ├── campaigns.html    # ⚠️ #58: cloud twin of frontend/agent/templates/campaigns.html
│       │   └── access-denied.html
│       └── static/
│           ├── css/cloud.css    # admin-card-grid, health-stat cards, role-grid, admin wordmark
│           └── js/             # admin-dashboard.js + leads.js/campaigns.js (⚠️ #58: ~600 lines duplicated
│                               #   from frontend/agent/static/js; the XSS-escaping fix landed only here)
│
├── agent/                     # THE LOCAL APP (per machine) — crawler + standalone BFF + launcher.
│   │                          # Zero cloud.* imports (import-linter enforced, both directions)
│   ├── api.py                 # Job routes: POST /api/jobs, /api/jobs/{id}/resume, .../cancel — mounted
│   │                          #   into agent/bff/app.py; loopback + local-session + CSRF gated
│   ├── identity.py            # The operator's standing session: self-refreshing token via /auth/refresh
│   │                          #   + OS keyring + cached effective permissions; OperatorContext for templates
│   ├── localdb.py             # Local SQLite (agent_local.db): local_settings (cloud_api_base_url, agent_id)
│   │                          #   + visited_history (recrawl protection) — never synced to the cloud
│   ├── state.py                # Agent-owned config/browser/active_tasks, set by agent/bff/app.py's lifespan
│   ├── cloud_client.py        # CloudApiClient + create_remote_job/resume_remote_job + outbox flusher (leads
│   │                          #   only) + request_with_retry (shared retry-on-401 helper, also used by proxy.py)
│   ├── local_store.py         # LocalOutbox: durable SQLite (outbox, outbox_dead, frontier) — per-job
│   ├── bff/                   # The standalone local BFF app (plan.md §19.1 Phase 9 Part 2)
│   │   ├── app.py              # create_app(config): fresh FastAPI, owns Playwright, mounts everything below
│   │   ├── security.py        # require_loopback, require_local_session, verify_local_csrf (+ Host check)
│   │   ├── local_auth.py      # /auth/login (relay), /local-bootstrap, /auth/logout, /auth/me
│   │   ├── local_system.py    # /api/system/activity|cancel-all, /api/logs — this machine's own view
│   │   ├── pages.py            # Renders frontend/ templates locally (no admin dashboard)
│   │   └── proxy.py            # One generic reverse-proxy for every remaining /api/* shared-data route
│   ├── crawler/
│   │   ├── engine.py          # CrawlerEngine: priority queue, httpx/playwright, checkpoint, orchestration
│   │   ├── pagination.py      # Stateless pagination-link classifiers (is_pagination_link, safe_int, ...)
│   │   └── parser.py          # 6-stage lead-extraction pipeline + Lead dataclass + parse_for_engine
│   └── launcher/
│       ├── app.py             # CrawlerLauncher (AppState machine) + LoginDialog (logs in directly against
│       │                      #   the cloud) + first-run cloud-URL prompt; keyring; drain shutdown
│       ├── tray.py            # TrayController (pystray)
│       └── notifications.py   # notify() — notifypy toasts (cross-platform)
│
├── portal/                    # Thin entry-point + config shim (NOT the old monolith) — the ONE place
│   │                          #   allowed to import both cloud.* and agent.*, since it's the composition
│   │                          #   root, not part of either tier's runtime
│   ├── __main__.py            # `python -m portal` → portal.main.main()
│   ├── main.py                # cloud CLI: serve/import/import-json/crawl (debug)/create-admin
│   ├── config.py              # load_config() (cloud, + env overrides) / load_agent_config() (agent) —
│   │                          #   two separate config files, only this loader module is shared
│   ├── paths.py               # path resolution + first-run bootstrap (dev + PyInstaller frozen)
│   ├── default_config.yaml    # cloud's shipped config template (config.yaml is the gitignored live copy)
│   └── default_agent_config.yaml  # agent's shipped template (agent_config.yaml is its live copy) — just
│                              #   api.host/port; everything else the agent needs lives in agent/localdb.py
│
├── GovScraper/                # Standalone dev-time CLI, fully decoupled from cloud/agent/shared — its
│   │                          # API-calling code is duplicated (inlined) into cloud/services/importer.py,
│   │                          # this package is only for regenerating gov_domains.json by hand
│   ├── runner.py              # CLI: `python runner.py [out.json] [--category] [--org-type]`
│   ├── README.md
│   └── api/                   # api.py, config.py, extractor.py, __init__.py, docs.md
│
├── alembic/
│   ├── env.py                 # targets `from cloud.db import Base`; honors DATABASE_URL env
│   └── versions/              # 0000_add_core_tables … 0023_drop_visited_and_frontier (chain head)
│
├── deploy/                    # Production VPS deployment
│   ├── docker-compose.yml     # db · migrate · api · dispatcher · proxy
│   ├── Dockerfile             # Plain python:3.11-slim — no Playwright, no agent/ code; the cloud tier
│   │                          #   is genuinely crawler-free
│   ├── Caddyfile              # reverse_proxy api:8001 + automatic TLS
│   ├── config.docker.yaml     # container-tuned config (baked to portal/config.yaml)
│   ├── .env.example           # secrets + env template
│   ├── SECURITY.md            # hardening checklist + rotation runbooks
│   ├── BACKUP.md              # daily pg_dump + rehearsed restore (RPO ≤24h)
│   ├── PITR.md                # WAL archiving + point-in-time recovery (RPO minutes)
│   ├── backup.sh · restore.sh · harden-vps.sh
│
├── scripts/
│   ├── migrate_sqlite_to_pg.py        # one-time SQLite→Postgres data migration (PK remap)
│   ├── rotate_credential_encryption_key.py  # re-encrypt SMTP creds under a new key
│   ├── generate_version_info.py       # PyInstaller Windows version resource from a git tag
│   └── fault_injection_check.md       # manual resilience acceptance runbook
│
├── tests/                     # Split by tier, mirroring shared/cloud/agent
│   ├── shared/                # test_imports.py (portal.main), test_config.py (env-override behavior)
│   ├── cloud/                 # test_imports.py (cloud.api.server)
│   └── agent/                 # test_imports.py (agent.api)
│
├── .github/workflows/
│   ├── ci.yaml                # lint (diff-scoped) · import-sanity · pytest · import-boundaries (both
│   │                          #   agent⊥cloud directions) · migration smoke test
│   └── release.yaml           # tag-triggered PyInstaller build/release (win/mac/linux)
│
├── assets/favicon.ico
└── .docs/                     # This documentation tree
```

## Where the old `portal/` code went

The pre-overhaul monolith lived entirely under `portal/`. It was split by tier:

| Old location                        | New location                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `portal/api/*` (data routers)       | `cloud/api/*`                                                                |
| `portal/api/jobs.py` (job creation) | `agent/api.py` (local BFF)                                                   |
| `portal/db/*`                       | `cloud/db/*`                                                                 |
| `portal/crawler/*`                  | `agent/crawler/*`                                                            |
| `portal/services/lead_scoring.py`   | `shared/scoring.py`                                                          |
| `portal/db/enums.py`                | `shared/enums.py` (re-exported by `cloud/db/enums.py`)                       |
| `launcher/` (repo root)             | `agent/launcher/`                                                            |
| `portal/frontend/`                  | `frontend/` (hoisted from `cloud/frontend/` in Phase 7, plan.md §19.1)       |
| `cloud/scraper/importer.py`         | `cloud/services/importer.py` (Phase 7 — GovScraper's live-API calls inlined) |
| `portal/main.py`, `portal/paths.py` | unchanged (the surviving shim)                                               |

## Generated / ignored paths

| Path                                                    | Why excluded from git                                       |
|---------------------------------------------------------|-------------------------------------------------------------|
| `portal/data/govcrawler.db`                             | Runtime SQLite DB (cloud, desktop/dev)                      |
| `portal/data/agent_local.db`                            | Agent-local settings + visited history (`agent/localdb.py`) |
| `portal/data/outbox_job_*.db`                           | Per-job durable outbox                                      |
| `portal/data/portal.log`                                | Runtime log                                                 |
| `portal/config.yaml`                                    | User-edited live config                                     |
| `playwright_browsers/`                                  | ~600 MB Chromium                                            |
| `deploy/.env`, `deploy/backups/`, `deploy/wal_archive/` | Secrets + backup artefacts                                  |
| `dist/`, `build/`, `**/__pycache__/`, `venv/`           | Build/temp/env                                              |
