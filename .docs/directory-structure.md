# Directory Structure

Annotated file tree for the GovCrawler project. Runtime artefacts (`__pycache__`, `playwright_browsers/`,
`portal/data/`) and IDE folders are omitted.

```
GovCrawler/
│
├── run.py                          # Tkinter GUI Control Panel (CrawlerLauncher)
│                                   # Also handles headless browser install via
│                                   # "INSTALL_BROWSERS" argv sentinel
│
├── requirements.txt                # Python package dependencies
├── GovCrawler.spec                 # PyInstaller spec — builds Windows .exe
├── alembic.ini                     # Alembic config; env.py points at portal.db.Base
├── README.md                       # Project overview and quick start
│
├── .docs/                          # Internal documentation (this folder)
│   ├── architecture.md             # System design and data flow
│   ├── directory-structure.md      # This file
│   ├── api-reference.md            # All REST endpoints
│   ├── database-schema.md          # ORM models and column reference
│   ├── crawler.md                  # Crawler engine internals
│   ├── outreach.md                 # Email campaign system
│   └── configuration.md            # Full config file reference
│
├── alembic/                        # Database migration scripts
│   ├── env.py                      # Migration environment; `from portal.db import Base`
│   └── versions/
│       ├── 0001_add_outreach_models.py    # campaigns, email_templates, smtp_credentials,
│       │                                  # campaign_emails, blacklist tables
│       ├── 0002_patch_campaign_fields.py  # template_id FK on campaigns
│       ├── 0003_add_test_campaign_models.py  # test_campaigns, test_campaign_emails
│       ├── 0004_add_email_selection.py    # is_selected + missing_fields columns
│       ├── 0005_add_lead_depth.py         # leads.depth (branch A)
│       ├── 0005_add_lead_grading.py       # entity_kind, phone, channel_tag,
│       │                                  # confidence_band, field_provenance (branch B)
│       ├── 0006_add_job_custom_urls.py    # crawl_jobs.source_type + job_custom_urls table
│       └── 0007_add_domain_external_id.py # domains.external_id (null-url dedup key)
│
├── GovScraper/                     # Standalone domain-discovery tool (no dependency on
│   │                               # the portal app/DB; run with GovScraper/ as CWD since
│   │                               # its modules use bare imports, e.g. `from api import ...`)
│   ├── __init__.py                 # Package root (empty — only GovScraper.api is a package)
│   ├── README.md                   # GovScraper-specific documentation + CLI usage
│   └── api/
│       ├── __init__.py             # Re-exports: get_categories, get_organization_types,
│       │                           # get_entries_for_category, HEADERS, TARGET_SUFFIXES,
│       │                           # extract_from_entries
│       ├── api.py                  # HTTP calls to india.gov.in Web Directory API
│       │                           # Functions: get_categories(), get_organization_types(),
│       │                           # get_entries_for_category() (paginated)
│       ├── config.py               # Constants: WEB_DIR_API URL, PAGE_SIZE, HEADERS,
│       │                           # TARGET_SUFFIXES (.gov.in, .nic.in, .res.in, .ac.in)
│       ├── extractor.py            # extract_from_entries() — groups full entry records
│       │                           # (title, url, contact_url, external_id) into
│       │                           # {state → {org_type → [record, ...]}}; url is None
│       │                           # for entries with no valid target-domain URL instead
│       │                           # of dropping them
│       ├── docs.md                 # API shape documentation
│       └── runner.py               # run_all_domains(config) → flat dict keyed by
│                                   # external_id (npi_sanitized_id): {title, url,
│                                   # contact_url, category, state, org_type}
│                                   # build_gov_domains_json(config) → nested dict ready
│                                   # to json.dump as gov_domains.json
│                                   # main() — CLI entry point (`python runner.py [output]
│                                   # [--category] [--org-type]`), invoked via `if __name__`
│
└── portal/                         # Core FastAPI application package
    │
    ├── __init__.py                 # Empty package marker
    ├── __main__.py                 # python -m portal entry: calls portal.main.main()
    │
    ├── main.py                     # CLI dispatcher + server factory
    │                               # Functions: load_config(), cmd_serve(), cmd_import(),
    │                               # cmd_import_json(), cmd_crawl(), main()
    │                               # Calls portal.paths.bootstrap() on import
    │
    ├── paths.py                    # Path resolution + first-run bootstrap
    │                               # get_app_dir(), get_bundle_dir(), bootstrap()
    │                               # APP_DIR, DATA_DIR, LOG_FILE_PATH, LIVE_CONFIG_PATH,
    │                               # BROWSER_PATH, DEFAULT_CONFIG_PATH
    │                               # Handles both dev and PyInstaller frozen modes;
    │                               # shared by portal/main.py and run.py
    │
    ├── default_config.yaml         # Shipped defaults (read-only in .exe mode)
    ├── config.yaml                 # Live user config; overrides defaults (gitignored)
    │
    ├── api/                        # REST API layer — one APIRouter per concern
    │   ├── __init__.py
    │   │
    │   ├── server.py               # FastAPI app factory: create_app(config, db)
    │   │                           # Pure app wiring only — no route logic:
    │   │                           # - Playwright browser lifespan (start/stop)
    │   │                           # - Static file mount (/static)
    │   │                           # - Wires deps._db/_config/_config_path
    │   │                           # - app.include_router(...) × 10
    │   │
    │   ├── deps.py                 # Shared app state + FastAPI dependency providers
    │   │                           # _db, _config, _config_path, _browser,
    │   │                           # _playwright_instance, _active_tasks
    │   │                           # get_db(), get_config(), get_config_path(),
    │   │                           # get_browser(), get_active_tasks()
    │   │                           # Routes pull state via Depends(...) instead of closures
    │   │
    │   ├── frontend.py             # HTML page routes + small UI-support endpoints
    │   │                           # /, /leads, /settings, /test-campaign, /campaigns,
    │   │                           # /user-guide, GET /api/logs, DELETE /api/visited-urls
    │   │                           # Owns the Jinja2Templates(frontend/) instance
    │   │
    │   ├── domains.py              # Domain metadata + browsing routes
    │   │                           # /api/categories, /api/states, /api/org-types,
    │   │                           # /api/domains, /api/domains/ids, /api/domains/stats,
    │   │                           # PATCH /api/domains/{id} (set a no-URL domain's URL)
    │   │
    │   ├── config.py               # Crawler/extraction settings routes
    │   │                           # GET/POST /api/config — reads/writes config.yaml
    │   │
    │   ├── imports.py              # Domain import routes
    │   │                           # /api/import/json, /api/import, /api/import/status
    │   │                           # _run_json_import()/_run_import() background tasks
    │   │
    │   ├── jobs.py                 # Crawl job routes
    │   │                           # /api/jobs, /api/jobs/{id}, /api/jobs/{id}/seeds,
    │   │                           # /api/jobs/{id}/cancel
    │   │                           # _run_crawl() background task (drives CrawlerEngine)
    │   │
    │   ├── leads.py                # Lead browsing, export, and editing routes
    │   │                           # /api/leads, /api/leads/ids, /api/leads/categories,
    │   │                           # /api/leads/states, /api/leads/export, PUT /api/leads/{id}
    │   │
    │   ├── campaigns.py            # Campaign generation + management routes (APIRouter)
    │   │                           # - Draft generation via services/campaign_service.py
    │   │                           # - Status management (RUNNING/PAUSED/CANCELLED)
    │   │                           # - Email editing, selection, deletion
    │   │                           # - Test campaign routes (same structure, dummy data)
    │   │
    │   ├── dispatcher.py           # Async SMTP dispatch background tasks
    │   │                           # run_campaign_dispatch(campaign_id, db)
    │   │                           # run_test_campaign_dispatch(campaign_id, db)
    │   │                           # - Flips DRAFT → QUEUED → SENT/FAILED
    │   │                           # - Round-robin credential selection
    │   │                           # - Hard-bounce → blacklist, rate-limit → cooldown
    │   │
    │   ├── credentials.py          # SMTP credential CRUD (APIRouter)
    │   │                           # - Password masking on list
    │   │                           # - Live SMTP connection test
    │   │
    │   ├── templates.py            # Email template CRUD (APIRouter)
    │   │                           # - Jinja2 syntax validation on create/update
    │   │
    │   └── blacklist.py            # Email/domain blacklist CRUD (APIRouter)
    │
    ├── services/                   # Business logic shared across route handlers
    │   ├── __init__.py
    │   └── campaign_service.py     # render_template_string(), render_draft_emails()
    │                               # Blacklist/exclude filtering + Jinja2 rendering +
    │                               # missing-field detection, used by campaigns.py's
    │                               # create_campaign and add_emails_to_campaign
    │
    ├── crawler/
    │   ├── __init__.py
    │   │
    │   ├── engine.py               # CrawlerEngine class
    │   │                           # - asyncio.PriorityQueue for URL scheduling
    │   │                           # - HTTPX-first fetch, Playwright fallback
    │   │                           # - Per-domain politeness locking
    │   │                           # - Recrawl protection via visited set
    │   │                           # - Offloads HTML parsing + DB writes to thread pools
    │   │                           #   (thread-pool target: parser.parse_for_engine)
    │   │                           # - _reporter() task: pushes metrics to DB every 2 s
    │   │
    │   └── parser.py               # Lead/email extraction
    │                               # Lead dataclass: email, person_name, designation,
    │                               #   department, source_url, source_title, context_snippet
    │                               # extract_leads(soup, url, config) — two-pass:
    │                               #   Pass 1: table rows (structured, high confidence)
    │                               #   Pass 2: proximity scan (email anchor → nearby name)
    │                               # parse_for_engine(html, url, excfg) — CrawlerEngine's
    │                               #   thread-pool target: builds the soup once, harvests
    │                               #   links, then calls extract_leads()
    │                               # NOTE: Phone extraction is intentionally excluded
    │
    ├── db/                         # SQLAlchemy models + Database access wrapper
    │   ├── __init__.py             # Re-exports Base, Database, enums, and all table classes
    │   ├── base.py                 # declarative_base() + SQLite WAL pragma listener
    │   ├── enums.py                # CampaignStatus, EmailStatus
    │   ├── database.py             # Database class: __init__, _ensure_columns(), close()
    │   │                           # Composed from the mixins below — same public API
    │   │                           # as before, just organized by concern
    │   │
    │   ├── tables/                 # ORM model definitions
    │   │   ├── __init__.py
    │   │   ├── crawl.py            # Domain, CrawlJob, VisitedUrl
    │   │   ├── leads.py            # Lead
    │   │   └── outreach.py         # Campaign, CampaignEmail, EmailTemplate,
    │   │                           # SMTPCredential, Blacklist, TestCampaign,
    │   │                           # TestCampaignEmail
    │   │
    │   └── mixins/                 # Database's methods, grouped by concern
    │       ├── __init__.py
    │       ├── domain_mixin.py     # upsert_domain, update_domain_url, get_domain_stats,
    │       │                       # get_domains, get_categories, get_states, get_org_types,
    │       │                       # get_domain_ids, get_domains_by_ids, clear_domains,
    │       │                       # count_domains
    │       ├── job_mixin.py        # create_job, start_job, finish_job,
    │       │                       # increment_job_progress, update_job_metrics,
    │       │                       # get_job, list_jobs, _job_dict
    │       ├── lead_mixin.py       # save_lead, get_leads, get_lead_ids,
    │       │                       # get_all_leads_for_export, get_lead_categories,
    │       │                       # get_lead_states, update_lead
    │       │                       # _apply_lead_filters(): shared filter-building
    │       │                       # helper used by all three list/export methods
    │       │                       # so pagination totals can never diverge from rows
    │       ├── visited_mixin.py    # mark_visited, get_visited_urls,
    │       │                       # get_recently_visited_global, clear_visited_urls
    │       └── outreach_mixin.py   # Templates, blacklist, campaigns, campaign emails,
    │                               # credentials, test campaigns (largest mixin)
    │
    ├── scraper/
    │   ├── __init__.py
    │   └── importer.py             # Domain import into portal DB
    │                               # import_from_json(db, path, config)
    │                               # import_all(db, config)  ← live API
    │                               # import_status dict — polled by /api/import/status
    │
    ├── frontend/                   # Web UI
    │   ├── base.html               # Base Jinja2 layout: navbar, footer, CSS/JS links
    │   ├── index.html              # Domains browser + crawl job creation + live job status
    │   ├── leads.html              # Lead table: filter, search, inline edit, CSV export
    │   ├── campaigns.html          # Campaign list + creation + email staging + dispatch
    │   ├── settings.html           # Crawler config editor (live YAML edit)
    │   ├── test-campaign.html      # Test campaign creation with dummy recipients
    │   ├── user-guide.html         # In-app user guide
    │   └── static/
    │       ├── css/
    │       │   ├── base.css        # CSS variables, layout, navbar, modals
    │       │   ├── leads.css       # Lead table, edit overlay
    │       │   ├── campaigns.css   # Campaign cards, email table, status badges
    │       │   └── settings.css    # Config form sections, textarea inputs
    │       └── js/
    │           ├── base.js         # Shared: apiFetch(), debounce, modal helpers
    │           ├── leads.js        # Lead table rendering, edit forms, export
    │           ├── campaigns.js    # Campaign creation, email review, dispatch polling
    │           ├── settings.js     # Config form submit, SMTP credential test
    │           └── test-campaign.js # Test campaign form handling
    │
    └── data/                       # Created on first run (gitignored)
        ├── govcrawler.db           # SQLite database file
        └── portal.log              # Application log (last 1000 lines via /api/logs)
```

## Notable Files

| File                                  | Size       | Notes                                                      |
|---------------------------------------|------------|-------------------------------------------------------------|
| `portal/db/mixins/outreach_mixin.py`  | ~500 lines | Largest single db file — templates through test campaigns |
| `portal/crawler/parser.py`            | ~670 lines | Extraction pipeline + `parse_for_engine` thread-pool entry point |
| `portal/crawler/engine.py`            | ~530 lines | Core async crawler implementation                          |
| `portal/api/campaigns.py`             | ~510 lines | Campaign creation, staging, dispatch routes (APIRouter)    |
| `portal/api/dispatcher.py`            | ~300 lines | SMTP dispatch loop for both real + test campaigns          |
| `portal/scraper/importer.py`          | ~330 lines | JSON and live-API import                                   |
| `portal/db/mixins/lead_mixin.py`      | ~190 lines | Lead CRUD + shared `_apply_lead_filters` helper            |
| `portal/api/leads.py`                 | ~140 lines | Lead browsing, export, and editing routes                  |
| `portal/api/jobs.py`                  | ~140 lines | Crawl job routes + `_run_crawl` background task             |
| `portal/db/mixins/domain_mixin.py`    | ~215 lines | Domain CRUD, stats, and metadata queries                     |
| `GovScraper/api/api.py`               | ~110 lines | Three HTTP functions for india.gov.in API                   |
| `portal/api/server.py`                | ~65 lines  | Pure app wiring — lifespan + `include_router` × 10          |
| `run.py`                              | ~230 lines | Tkinter GUI + INSTALL_BROWSERS sentinel                     |

## Generated / Ignored Paths

| Path                        | Why excluded from git           |
|-----------------------------|---------------------------------|
| `portal/data/govcrawler.db` | Runtime database, user-specific |
| `portal/data/portal.log`    | Runtime log output              |
| `portal/config.yaml`        | User-edited live config         |
| `playwright_browsers/`      | ~600 MB Chromium binary         |
| `dist/`                     | PyInstaller build output        |
| `build/`                    | PyInstaller build temp          |
| `**/__pycache__/`           | Python bytecode                 |
| `venv/`                     | Virtual environment             |
