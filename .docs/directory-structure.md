# Directory Structure

Annotated file tree for the GovCrawler project. Runtime artefacts (`__pycache__`, `playwright_browsers/`,
`portal/data/`) and IDE folders are omitted.

```
GovCrawler/
│
├── run.py                          # Thin GUI entry point: SSL/cert fixes, the
│                                   # "INSTALL_BROWSERS" argv sentinel (headless
│                                   # browser install), and the __main__ block that
│                                   # hands off to launcher.app.CrawlerLauncher
│
├── launcher/                        # Tkinter Control Panel (not part of portal/ —
│   │                                 # this is the desktop wrapper, not the web app)
│   ├── __init__.py
│   ├── app.py                      # CrawlerLauncher: AppState machine, sv-ttk UI,
│   │                               # HTTP polling of /api/system/activity, the
│   │                               # confirm → cancel-all → drain → shutdown flow
│   ├── tray.py                     # TrayController: pystray icon + its own thread
│   └── notifications.py            # notify() — thin notifypy wrapper (cross-platform;
│                                   # NOT winotify, which crashes on macOS/Linux)
│
├── assets/                          # Desktop app icon, shared by window/taskbar,
│   └── favicon.ico                 # tray icon, and the compiled .exe (GovCrawler.spec)
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
│       ├── 0007_add_domain_external_id.py # domains.external_id (null-url dedup key)
│       ├── 0008_add_campaign_credentials.py  # campaign_credentials table,
│       │                                     # smtp_credentials.daily_send_limit,
│       │                                     # campaign_emails/test_campaign_emails.credential_id
│       ├── 0009_add_campaign_pause_reason.py # campaigns/test_campaigns.pause_reason
│       ├── 0010_add_lead_score.py            # leads.lead_score (values populated by
│       │                                     # Database._recompute_lead_scores(), not the migration)
│       └── 0011_add_crawl_snapshots.py       # crawl_snapshots table only — leads.snapshot_id
│                                             # column + backfill deliberately live in
│                                             # Database._ensure_columns()/_backfill_snapshots()
│                                             # instead (see that file's docstring for why)
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
    │                               # BROWSER_PATH, DEFAULT_CONFIG_PATH, ICON_PATH
    │                               # Handles both dev and PyInstaller frozen modes;
    │                               # shared by portal/main.py and launcher/app.py
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
    │   │                           # - app.include_router(...) × 11
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
    │   │                           # Seeds via domain_ids OR custom_urls (mutually
    │   │                           # exclusive); custom_urls validated/deduped/capped
    │   │                           # by crawler.max_custom_urls and bypass target_suffixes
    │   │                           # _run_crawl() background task (drives CrawlerEngine)
    │   │
    │   ├── leads.py                # Lead browsing, export, and editing routes
    │   │                           # /api/leads, /api/leads/ids, /api/leads/score-weights,
    │   │                           # /api/leads/categories, /api/leads/states,
    │   │                           # /api/leads/org-types, /api/leads/export,
    │   │                           # /api/leads/import-csv(/template), PUT /api/leads/{id}
    │   │
    │   ├── campaigns.py            # Campaign generation + management routes (APIRouter)
    │   │                           # - Draft generation via services/campaign_service.py
    │   │                           # - Status management (RUNNING/PAUSED/CANCELLED)
    │   │                           # - Per-campaign SMTP credential assignment
    │   │                           #   (PUT /api/campaigns/{id}/credentials)
    │   │                           # - Email editing, selection (incl. bulk selection-all)
    │   │                           # - Test campaign routes (same structure, dummy data,
    │   │                           #   + parse-csv for dummy_details)
    │   │
    │   ├── dispatcher.py           # Async SMTP dispatch background tasks
    │   │                           # run_campaign_dispatch(campaign_id, db)
    │   │                           # run_test_campaign_dispatch(campaign_id, db)
    │   │                           # - Flips DRAFT → QUEUED → SENT/FAILED
    │   │                           # - resolve_credential_pool(): assigned credentials
    │   │                           #   (else all active), excluding daily_send_limit-capped
    │   │                           # - Round-robin over that pool; per-credential send
    │   │                           #   pacing shared across all campaigns in the process
    │   │                           # - Hard-bounce → blacklist; rate-limit/network error
    │   │                           #   → cooldown; auth failure → disable credential
    │   │                           # - No usable credential → campaign PAUSED + pause_reason
    │   │
    │   ├── credentials.py          # SMTP credential CRUD (APIRouter)
    │   │                           # - Password masking on list
    │   │                           # - daily_send_limit on create/update
    │   │                           # - Live SMTP connection test (auto-disables on
    │   │                           #   failure, re-activates on success)
    │   │
    │   ├── templates.py            # Email template CRUD (APIRouter)
    │   │                           # - Jinja2 syntax validation on create/update
    │   │
    │   ├── blacklist.py            # Email/domain blacklist CRUD (APIRouter)
    │   │
    │   └── system.py               # Activity aggregation for the desktop Control Panel
    │                               # GET /api/system/activity, POST /api/system/cancel-all
    │                               # Reads deps._active_tasks + campaigns._active_campaign_tasks
    │                               # directly (ground truth); test campaigns via DB status only
    │
    ├── services/                   # Business logic shared across route handlers
    │   ├── __init__.py
    │   ├── campaign_service.py     # render_template_string(), render_draft_emails()
    │   │                           # Blacklist/exclude filtering + Jinja2 rendering +
    │   │                           # missing-field detection, used by campaigns.py's
    │   │                           # create_campaign and add_emails_to_campaign
    │   ├── csv_import.py           # parse_contacts_csv(), build_template_csv() — shared
    │   │                           # by leads.py's import-csv and campaigns.py's
    │   │                           # test-campaigns/parse-csv
    │   └── lead_scoring.py         # compute_lead_score(), DEFAULT_WEIGHTS — 0-100 lead
    │                               # score from email confidence_band + name/designation/
    │                               # phone presence; manual (channel_tag="manual") leads
    │                               # always score 0
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
    │   └── parser.py               # Lead/email/phone extraction — 6-stage pipeline
    │                               # Lead dataclass: email, person_name, designation,
    │                               #   department, source_url, source_title, context_snippet,
    │                               #   entity_kind, phone, channel_tag, confidence_band,
    │                               #   field_provenance
    │                               # extract_leads(soup, url, config):
    │                               #   1. extract_candidates — mailto:/tel: hrefs, microdata
    │                               #      itemprop, table rows, proximity-text regex scan
    │                               #   2. bind_channels — group by email into entities;
    │                               #      classify channel_tag (office/personal-external/role)
    │                               #   3. enrich_fields — name/designation/department per entity
    │                               #   4. normalise_spans — guarded de-obfuscation, bracketed
    │                               #      forms only (never a global text rewrite)
    │                               #   5. score — confidence_band (HIGH/LOW) + field_provenance
    │                               #   6. flatten_emit — one Lead per email; band never drops one
    │                               # parse_for_engine(html, url, excfg) — CrawlerEngine's
    │                               #   thread-pool target: builds the soup once, harvests
    │                               #   links (incl. rel="next" for pagination), then calls
    │                               #   extract_leads()
    │
    ├── db/                         # SQLAlchemy models + Database access wrapper
    │   ├── __init__.py             # Re-exports Base, Database, enums, and all table classes
    │   ├── base.py                 # declarative_base() + SQLite WAL pragma listener
    │   ├── enums.py                # CampaignStatus, EmailStatus
    │   ├── database.py             # Database class: __init__, _ensure_columns()
    │   │                           # (incl. _recompute_lead_scores(), _backfill_snapshots()),
    │   │                           # close(). Composed from the mixins below — same public
    │   │                           # API as before, just organized by concern
    │   ├── migrations.py           # run_migrations(db_uri): stamps a pre-Alembic DB at
    │   │                           # head (first contact), then always runs
    │   │                           # `alembic upgrade head` — called from Database.__init__
    │   │                           # after _ensure_columns(), every startup
    │   │
    │   ├── tables/                 # ORM model definitions
    │   │   ├── __init__.py
    │   │   ├── crawl.py            # Domain, CrawlJob, CrawlSnapshot, VisitedUrl, JobCustomUrl
    │   │   ├── leads.py            # Lead
    │   │   └── outreach.py         # Campaign, CampaignCredential, CampaignEmail,
    │   │                           # EmailTemplate, SMTPCredential, Blacklist,
    │   │                           # TestCampaign, TestCampaignEmail
    │   │
    │   └── mixins/                 # Database's methods, grouped by concern
    │       ├── __init__.py
    │       ├── domain_mixin.py     # upsert_domain, update_domain_url, get_domain_stats,
    │       │                       # get_domains, get_categories, get_states, get_org_types,
    │       │                       # get_domain_ids, get_domains_by_ids, clear_domains,
    │       │                       # count_domains
    │       ├── job_mixin.py        # create_job (domain_ids or custom_urls), start_job,
    │       │                       # finish_job, increment_job_progress, update_job_metrics,
    │       │                       # get_job, list_jobs, _job_dict,
    │       │                       # add_job_custom_urls, get_job_custom_urls
    │       ├── crawl_snapshot_mixin.py  # create_crawl_snapshot (get-or-insert, per
    │       │                       # (job_id, source_domain_id)), get_crawl_snapshots(job_id)
    │       │                       # — freezes seed domain metadata per crawl so leads
    │       │                       # (and a job's seed view) are immune to domains refreshes
    │       ├── lead_mixin.py       # get_lead_score_weights, save_lead, get_leads,
    │       │                       # get_lead_ids, get_all_leads_for_export,
    │       │                       # get_lead_categories, get_lead_states, get_lead_org_types,
    │       │                       # bulk_upsert_manual_leads, update_lead
    │       │                       # Domain-derived fields (title, category, state, org_type)
    │       │                       # are read via CrawlSnapshot (joined on Lead.snapshot_id),
    │       │                       # not the live domains catalog — see database-schema.md
    │       │                       # _apply_lead_filters(): shared filter-building
    │       │                       # helper used by all three list/export methods
    │       │                       # so pagination totals can never diverge from rows
    │       │                       # _apply_lead_sort(): separate helper for score/contact/name
    │       │                       # sort — deliberately not folded into the filter helper
    │       ├── visited_mixin.py    # mark_visited, get_visited_urls,
    │       │                       # get_recently_visited_global, clear_visited_urls
    │       └── outreach_mixin.py   # Templates, blacklist, campaigns (incl. per-campaign
    │                               # credential assignment), campaign emails, credentials,
    │                               # test campaigns (largest mixin)
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
    │       ├── img/
    │       │   └── favicon.ico     # Browser tab icon (same source as assets/favicon.ico)
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

| File                                 | Size       | Notes                                                                          |
|--------------------------------------|------------|--------------------------------------------------------------------------------|
| `portal/api/campaigns.py`            | ~605 lines | Campaign + test-campaign creation, staging, dispatch routes (APIRouter)        |
| `portal/db/mixins/outreach_mixin.py` | ~500 lines | Largest single db file — templates through test campaigns                      |
| `portal/crawler/parser.py`           | ~670 lines | 6-stage extraction pipeline + `parse_for_engine` thread-pool entry point       |
| `portal/crawler/engine.py`           | ~530 lines | Core async crawler implementation, incl. pagination-chain logic                |
| `portal/api/dispatcher.py`           | ~330 lines | SMTP dispatch loop for both real + test campaigns                              |
| `portal/scraper/importer.py`         | ~330 lines | JSON and live-API import                                                       |
| `portal/db/mixins/lead_mixin.py`     | ~325 lines | Lead CRUD + shared `_apply_lead_filters`/`_apply_lead_sort` helpers            |
| `portal/api/leads.py`                | ~205 lines | Lead browsing, export, CSV import, and editing routes                          |
| `portal/api/jobs.py`                 | ~200 lines | Crawl job routes (domain- or custom-URL-seeded) + `_run_crawl` background task |
| `portal/db/mixins/domain_mixin.py`   | ~215 lines | Domain CRUD, stats, and metadata queries                                       |
| `GovScraper/api/api.py`              | ~110 lines | Three HTTP functions for india.gov.in API                                      |
| `launcher/app.py`                    | ~400 lines | CrawlerLauncher: state machine, polling, shutdown flow, UI                     |
| `portal/api/server.py`               | ~65 lines  | Pure app wiring — lifespan + `include_router` × 11                             |
| `portal/api/system.py`               | ~100 lines | Activity aggregation + cancel-all for the desktop GUI                          |
| `launcher/tray.py`                   | ~50 lines  | pystray icon lifecycle                                                         |
| `run.py`                             | ~50 lines  | Bootstrap only — SSL fix, INSTALL_BROWSERS sentinel, entry                     |

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
