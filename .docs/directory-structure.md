# Directory Structure

Annotated file tree for the GovCrawler project. Runtime artefacts (`__pycache__`, `playwright_browsers/`, `portal/data/`) and IDE folders are omitted.

```
GovCrawler/
│
├── run.py                          # Tkinter GUI Control Panel (CrawlerLauncher)
│                                   # Also handles headless browser install via
│                                   # "INSTALL_BROWSERS" argv sentinel
│
├── requirements.txt                # Python package dependencies
├── GovCrawler.spec                 # PyInstaller spec — builds Windows .exe
├── alembic.ini                     # Alembic config; points to portal/db/models.py
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
│   ├── env.py                      # Migration environment; imports Base from models.py
│   └── versions/
│       ├── 0001_add_outreach_models.py    # campaigns, email_templates, smtp_credentials,
│       │                                  # campaign_emails, blacklist tables
│       ├── 0002_patch_campaign_fields.py  # template_id FK on campaigns
│       ├── 0003_add_test_campaign_models.py  # test_campaigns, test_campaign_emails
│       └── 0004_add_email_selection.py    # is_selected + missing_fields columns
│
├── GovScraper/                     # Standalone domain-discovery library
│   ├── __init__.py                 # Package root
│   ├── README.md                   # GovScraper-specific documentation
│   └── api/
│       ├── __init__.py             # Re-exports: get_categories, get_organization_types,
│       │                           # get_entries_for_category, HEADERS, TARGET_SUFFIXES,
│       │                           # extract_from_entries
│       ├── api.py                  # HTTP calls to india.gov.in Web Directory API
│       │                           # Functions: get_categories(), get_organization_types(),
│       │                           # get_entries_for_category() (paginated)
│       ├── config.py               # Constants: WEB_DIR_API URL, PAGE_SIZE, HEADERS,
│       │                           # TARGET_SUFFIXES (.gov.in, .nic.in, .res.in, .ac.in)
│       ├── extractor.py            # extract_from_entries() — groups raw API entries
│       │                           # into {state → {org_type → [root_urls]}}
│       ├── docs.md                 # API shape documentation
│       └── runner.py               # run_all_domains(config) — standalone CLI runner;
│                                   # returns {url: {category, state, org_type}}
│
└── portal/                         # Core FastAPI application package
    │
    ├── __init__.py                 # Empty package marker
    ├── __main__.py                 # python -m portal entry: calls portal.main.main()
    │
    ├── main.py                     # CLI dispatcher + server factory
    │                               # Functions: load_config(), cmd_serve(), cmd_import(),
    │                               # cmd_import_json(), cmd_crawl(), main()
    │                               # Path management: get_app_dir(), get_bundle_dir()
    │                               # Handles both dev and PyInstaller frozen modes
    │
    ├── default_config.yaml         # Shipped defaults (read-only in .exe mode)
    ├── config.yaml                 # Live user config; overrides defaults (gitignored)
    │
    ├── api/                        # REST API layer
    │   ├── __init__.py
    │   │
    │   ├── server.py               # FastAPI app factory: create_app(config, db)
    │   │                           # - Playwright browser lifespan (start/stop)
    │   │                           # - Static file mount (/static)
    │   │                           # - Jinja2 template dir (frontend/)
    │   │                           # - Frontend page routes (/, /leads, /campaigns, ...)
    │   │                           # - Core API routes: domains, config, import, jobs, leads
    │   │                           # - Registers outreach routes from sub-modules
    │   │                           # Global state: _db, _config, _browser, _active_tasks
    │   │
    │   ├── campaigns.py            # Campaign generation + management routes
    │   │                           # register_campaign_routes(app, db)
    │   │                           # - Draft generation from leads + Jinja2 template
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
    │   ├── credentials.py          # SMTP credential CRUD
    │   │                           # register_credential_routes(app, db)
    │   │                           # - Password masking on list
    │   │                           # - Live SMTP connection test
    │   │
    │   ├── templates.py            # Email template CRUD
    │   │                           # register_template_routes(app, db)
    │   │                           # - Jinja2 syntax validation on create/update
    │   │
    │   └── blacklist.py            # Email/domain blacklist CRUD
    │                               # register_blacklist_routes(app, db)
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
    │   │                           # - _reporter() task: pushes metrics to DB every 2 s
    │   │
    │   └── parser.py               # Lead/email extraction
    │                               # Lead dataclass: email, person_name, designation,
    │                               #   department, source_url, source_title, context_snippet
    │                               # extract_leads(soup, url, config) — two-pass:
    │                               #   Pass 1: table rows (structured, high confidence)
    │                               #   Pass 2: proximity scan (email anchor → nearby name)
    │                               # NOTE: Phone extraction is intentionally excluded
    │
    ├── db/
    │   ├── __init__.py
    │   └── models.py               # All ORM models + Database wrapper class (~1200 lines)
    │                               # Models: Domain, CrawlJob, Lead, VisitedUrl,
    │                               #         EmailTemplate, Campaign, CampaignEmail,
    │                               #         SMTPCredential, Blacklist,
    │                               #         TestCampaign, TestCampaignEmail
    │                               # Enums: CampaignStatus, EmailStatus
    │                               # WAL pragma applied on SQLite connect
    │                               # _ensure_columns(): safe column addition for SQLite
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

| File | Size | Notes |
|---|---|---|
| `portal/db/models.py` | ~1200 lines | All ORM models + full Database wrapper class |
| `portal/crawler/engine.py` | ~570 lines | Core async crawler implementation |
| `portal/api/server.py` | ~553 lines | App factory + all core API routes inline |
| `portal/api/campaigns.py` | ~570 lines | Campaign creation, staging, dispatch routes |
| `portal/api/dispatcher.py` | ~297 lines | SMTP dispatch loop for both real + test campaigns |
| `portal/scraper/importer.py` | ~277 lines | JSON and live-API import |
| `portal/crawler/parser.py` | ~241 lines | Two-pass email + person extraction |
| `GovScraper/api/api.py` | ~111 lines | Three HTTP functions for india.gov.in API |
| `run.py` | ~197 lines | Tkinter GUI + INSTALL_BROWSERS sentinel |

## Generated / Ignored Paths

| Path | Why excluded from git |
|---|---|
| `portal/data/govcrawler.db` | Runtime database, user-specific |
| `portal/data/portal.log` | Runtime log output |
| `portal/config.yaml` | User-edited live config |
| `playwright_browsers/` | ~600 MB Chromium binary |
| `dist/` | PyInstaller build output |
| `build/` | PyInstaller build temp |
| `**/__pycache__/` | Python bytecode |
| `venv/` | Virtual environment |
