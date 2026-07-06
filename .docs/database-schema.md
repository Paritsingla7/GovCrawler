# Database Schema

Tables are defined as SQLAlchemy ORM models under [`portal/db/tables/`](../portal/db/tables/): `crawl.py` (Domain,
CrawlJob, CrawlSnapshot, VisitedUrl, JobCustomUrl), `leads.py` (Lead), and `outreach.py` (Campaign, CampaignEmail,
EmailTemplate, SMTPCredential, Blacklist, TestCampaign, TestCampaignEmail, CampaignCredential). Enums live in
[`portal/db/enums.py`](../portal/db/enums.py). The `Database` wrapper class in
[`portal/db/database.py`](../portal/db/database.py) provides all data-access methods — its ~50 methods are composed
from mixins under [`portal/db/mixins/`](../portal/db/mixins/), grouped by concern (domain, job, lead, visited-url,
outreach). Direct session usage elsewhere in the codebase is intentionally avoided.

**Backend:** SQLite (default, WAL mode) or PostgreSQL (set `database.uri` in config).

**Schema management — three layers, applied in this order on every startup** (`Database.__init__`,
`portal/db/database.py`):

1. `Base.metadata.create_all()` — creates any entirely missing tables.
2. `_ensure_columns()` — lightweight additive `ALTER TABLE ... ADD COLUMN` for columns added outside a formal
   migration. It also calls `_recompute_lead_scores()`, which recomputes every row's `leads.lead_score` using the
   current `lead_score.weights` config — so changing the weights and restarting the server retroactively re-scores
   every lead, not just newly-crawled ones.
3. `run_migrations()` (`portal/db/migrations.py`) — runs Alembic (`alembic/versions/`) automatically, every startup,
   for both SQLite and PostgreSQL. A database with no `alembic_version` table is stamped at `head` first (since
   `create_all()`/`_ensure_columns()` already cover everything up to that point), then `alembic upgrade head` runs
   unconditionally. There is no manual "run migrations" step in normal operation — see
   [configuration.md](configuration.md#postgresql-setup).

---

## Enumerations

### `CampaignStatus`

| Value       | Meaning                                             |
|-------------|-----------------------------------------------------|
| `RUNNING`   | Dispatch is active or campaign is ready to dispatch |
| `PAUSED`    | Dispatch is suspended (user action or rate limit)   |
| `CANCELLED` | All remaining queued emails marked FAILED           |
| `COMPLETED` | All selected emails sent (no DRAFT or QUEUED left)  |

### `EmailStatus`

| Value    | Meaning                                             |
|----------|-----------------------------------------------------|
| `DRAFT`  | Rendered, not yet queued for sending                |
| `QUEUED` | Flipped from DRAFT, waiting for dispatcher          |
| `SENT`   | Successfully sent                                   |
| `FAILED` | Send attempt failed (hard bounce, auth error, etc.) |

---

## Tables

### `domains`

Seed records from the india.gov.in Web Directory.

| Column           | Type       | Notes                                               |
|------------------|------------|-----------------------------------------------------|
| `id`             | Integer PK | Auto-increment                                      |
| `category_code`  | String     | e.g. `ug`, `sg`, `dist` — indexed                   |
| `category_title` | String     | Human-readable e.g. "Union Government"              |
| `state`          | String     | State/UT name — indexed                             |
| `org_type`       | String     | Organization type code — indexed                    |
| `org_type_title` | String     | e.g. "Departments", "Statutory Bodies"              |
| `title`          | String     | Organization name — indexed                         |
| `main_url`       | String     | Root URL (scheme + netloc only); nullable           |
| `contact_url`    | String     | Direct contact/directory page (nullable)            |
| `external_id`    | String     | india.gov.in `npi_sanitized_id` — indexed, nullable |
| `imported_at`    | DateTime   | UTC timestamp of last import                        |

`main_url`/`contact_url` are `null` for organizations the india.gov.in directory lists with no URL at all — these
are imported (not dropped) so their metadata isn't lost, and the frontend marks them "not crawlable" until a URL is
added via `PATCH /api/domains/{id}`.

**Upsert key:** `external_id` when present (the only stable key for entries with no `main_url`), otherwise
`main_url`. Rows with neither are always inserted fresh. See `upsert_domain()`.

---

### `crawl_jobs`

Tracks a single crawl run over a set of domains.

| Column            | Type       | Notes                                                                     |
|-------------------|------------|---------------------------------------------------------------------------|
| `id`              | Integer PK | Auto-increment                                                            |
| `domain_ids`      | Text       | JSON-serialized `list[int]` of seed domain IDs (null for custom-URL jobs) |
| `source_type`     | String     | `domains` (default) or `custom_urls` — which seeding path was used        |
| `category_filter` | String     | Optional filter label (metadata only)                                     |
| `title_filter`    | String     | Optional filter label (metadata only)                                     |
| `status`          | String     | `pending`, `running`, `done`, `failed`, `cancelled`                       |
| `total_domains`   | Integer    | Count of seed domains                                                     |
| `crawled_domains` | Integer    | Seed domains whose seed page was visited                                  |
| `seed_domains`    | Integer    | Same as `total_domains` (kept for display)                                |
| `queued_urls`     | Integer    | URLs currently in the priority queue                                      |
| `visited_urls`    | Integer    | URLs visited in this session                                              |
| `skipped_urls`    | Integer    | URLs skipped (recrawl, extension, non-gov, limit)                         |
| `leads_found`     | Integer    | Total leads extracted                                                     |
| `current_depth`   | Integer    | Deepest crawl depth reached so far                                        |
| `active_workers`  | Integer    | Workers currently processing a URL                                        |
| `error_message`   | String     | Set on `failed` status                                                    |
| `created_at`      | DateTime   | UTC                                                                       |
| `started_at`      | DateTime   | Set when status → `running`                                               |
| `finished_at`     | DateTime   | Set when status → `done`/`failed`/`cancelled`                             |

For `custom_urls` jobs, the actual seed URLs live in the separate `job_custom_urls` table (below), not
`domain_ids`. `GET /api/jobs/{id}/seeds` branches on `source_type` to return the right shape.

---

### `job_custom_urls`

Ad-hoc seed URLs for a `custom_urls`-sourced crawl job (an alternative to selecting known `domains` rows).

| Column       | Type       | Notes                          |
|--------------|------------|--------------------------------|
| `id`         | Integer PK | Auto-increment                 |
| `job_id`     | Integer FK | → `crawl_jobs.id` — indexed    |
| `url`        | String     | Normalized, deduped custom URL |
| `created_at` | DateTime   | UTC                            |

**Unique constraint:** `(job_id, url)`

---

### `crawl_snapshots`

Per-crawl **frozen copy** of a seed domain's metadata. Leads (and a job's seed view) point here instead of at the
mutable `domains` catalog, so refreshing/rebuilding `domains` (which reassigns `domains.id`) never alters
lead-visible data — the metadata is frozen exactly as it was when the crawl ran.

| Column             | Type       | Notes                                                       |
|--------------------|------------|-------------------------------------------------------------|
| `id`               | Integer PK | Auto-increment; threaded through the crawler as the seed id |
| `job_id`           | Integer FK | → `crawl_jobs.id` — indexed                                 |
| `source_domain_id` | Integer    | Catalog `domains.id` at crawl time (soft link, nullable)    |
| `external_id`      | String     | Frozen from the domain                                      |
| `category_code`    | String     | Frozen                                                      |
| `category_title`   | String     | Frozen                                                      |
| `state`            | String     | Frozen                                                      |
| `org_type`         | String     | Frozen                                                      |
| `org_type_title`   | String     | Frozen                                                      |
| `title`            | String     | Frozen                                                      |
| `main_url`         | String     | Frozen                                                      |
| `contact_url`      | String     | Frozen                                                      |
| `created_at`       | DateTime   | UTC                                                         |

**Unique constraint:** `(job_id, source_domain_id)`. `create_crawl_snapshot()` is **get-or-insert** — an existing
row for a job+domain is returned unchanged, never overwritten, so leads captured earlier stay frozen. Snapshots are
created at job-creation time (`POST /api/jobs`) and read back by both the crawler and `GET /api/jobs/{id}/seeds`.

---

### `leads`

Extracted contact records.

| Column             | Type       | Notes                                                                                                                                                                                           |
|--------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `id`               | Integer PK | Auto-increment                                                                                                                                                                                  |
| `job_id`           | Integer FK | → `crawl_jobs.id` — indexed                                                                                                                                                                     |
| `snapshot_id`      | Integer FK | → `crawl_snapshots.id` (nullable). **Source of all domain-derived display/filter fields** — immune to catalog refreshes. Null for manual/custom-URL leads.                                      |
| `domain_id`        | Integer FK | → `domains.id` (nullable). Soft historical link only; set from the snapshot's `source_domain_id`. No longer used for lead display, so a value that dangles after a catalog refresh is harmless. |
| `email`            | String     | Lowercase, indexed                                                                                                                                                                              |
| `person_name`      | String     | Extracted or user-edited (nullable)                                                                                                                                                             |
| `designation`      | String     | e.g. "Secretary", "Director" (nullable)                                                                                                                                                         |
| `department`       | String     | e.g. "Ministry of Finance" (nullable)                                                                                                                                                           |
| `source_url`       | String     | Page the lead was extracted from                                                                                                                                                                |
| `source_title`     | String     | `<title>` of that page (nullable)                                                                                                                                                               |
| `context_snippet`  | Text       | ±200 chars around the email (nullable)                                                                                                                                                          |
| `domain_state`     | String     | Denormalized from domain at insert time                                                                                                                                                         |
| `domain_org_type`  | String     | Denormalized from domain at insert time                                                                                                                                                         |
| `entity_kind`      | String     | Nullable; extraction classification                                                                                                                                                             |
| `phone`            | String     | Nullable; extracted phone number                                                                                                                                                                |
| `channel_tag`      | String     | Nullable; `"manual"` for CSV-imported leads, else extraction-set                                                                                                                                |
| `confidence_band`  | String     | Nullable; email provenance tier — see `extraction.confidence` in [configuration.md](configuration.md)                                                                                           |
| `field_provenance` | Text       | Nullable; detail on where each field came from                                                                                                                                                  |
| `lead_score`       | Integer    | NOT NULL, default 0 — see "Lead Scoring" below                                                                                                                                                  |
| `depth`            | Integer    | NOT NULL, default 0 — crawl depth the lead was found at                                                                                                                                         |
| `captured_at`      | DateTime   | UTC                                                                                                                                                                                             |

**Unique constraint:** `(job_id, email)` — an email is stored at most once per job. Global dedup (across jobs) is
enforced at the `email` level in `save_lead()`.

**Editable fields:** `person_name`, `designation`, `department`, `domain_state` (via `PUT /api/leads/{id}`).

**Lead Scoring:** `lead_score` (0–100) is computed by `portal/services/lead_scoring.compute_lead_score()` from
`confidence_band` (email) plus the presence of `person_name`, `designation`, and `phone`, using the weights in the
`lead_score.weights` config section (default: email HIGH 20 / other 10, name 40, designation 30, phone 10). Leads
with `channel_tag == "manual"` always score 0 — the score exists to prioritize crawled leads, not grade manual
entries. Scores are recomputed for every row on every server startup via `Database._recompute_lead_scores()`, so
editing `lead_score.weights` retroactively reprices existing leads. `GET /api/leads/score-weights` exposes the
active weights read-only for the frontend's score-breakdown tooltip.

---

### `visited_urls`

Crawl history for recrawl protection.

| Column       | Type       | Notes              |
|--------------|------------|--------------------|
| `id`         | Integer PK | Auto-increment     |
| `url`        | String     | Full URL — indexed |
| `job_id`     | Integer FK | → `crawl_jobs.id`  |
| `visited_at` | DateTime   | UTC                |

**Unique constraint:** `(url, job_id)`

**Purpose:** On a new job, URLs visited within `recrawl_days` in *any* prior job are pre-loaded into the in-memory
`_visited` set, preventing redundant re-crawls of non-seed domains.

---

### `email_templates`

Jinja2 email templates for campaigns.

| Column     | Type       | Notes                  |
|------------|------------|------------------------|
| `id`       | Integer PK | Auto-increment         |
| `name`     | String     | Display name           |
| `subject`  | String     | Jinja2 template string |
| `raw_body` | Text       | Jinja2 template string |

**Template variables available at render time:** `name`, `designation`

---

### `campaigns`

Production email outreach campaigns.

| Column         | Type       | Notes                                                                                                         |
|----------------|------------|---------------------------------------------------------------------------------------------------------------|
| `id`           | Integer PK | Auto-increment                                                                                                |
| `name`         | String     | Display name                                                                                                  |
| `template_id`  | Integer FK | → `email_templates.id` (nullable)                                                                             |
| `status`       | Enum       | `CampaignStatus`                                                                                              |
| `pause_reason` | String     | Nullable; set when the dispatcher auto-pauses (e.g. no usable SMTP credentials), cleared on any status change |
| `created_at`   | DateTime   | UTC                                                                                                           |

---

### `campaign_credentials`

Many-to-many: which SMTP credentials a campaign is allowed to dispatch through. Empty (no rows for a campaign) means
"every active credential" — see [outreach.md](outreach.md#credential-assignment).

| Column          | Type       | Notes                      |
|-----------------|------------|----------------------------|
| `id`            | Integer PK | Auto-increment             |
| `campaign_id`   | Integer FK | → `campaigns.id` — indexed |
| `credential_id` | Integer FK | → `smtp_credentials.id`    |

**Unique constraint:** `(campaign_id, credential_id)`

---

### `campaign_emails`

Individual staged email drafts for a production campaign.

| Column            | Type       | Notes                                                                          |
|-------------------|------------|--------------------------------------------------------------------------------|
| `id`              | Integer PK | Auto-increment                                                                 |
| `campaign_id`     | Integer FK | → `campaigns.id`                                                               |
| `lead_id`         | Integer FK | → `leads.id`                                                                   |
| `recipient_email` | String     | Lowercase email address                                                        |
| `subject`         | String     | Rendered from template                                                         |
| `body`            | Text       | Rendered from template                                                         |
| `status`          | Enum       | `EmailStatus`                                                                  |
| `is_selected`     | Boolean    | Default true; false = deselected (skipped on dispatch)                         |
| `missing_fields`  | String     | Comma-separated missing template vars (e.g. `name,designation`)                |
| `error_message`   | String     | Set on FAILED status (nullable)                                                |
| `credential_id`   | Integer FK | → `smtp_credentials.id` (nullable); which credential sent/attempted this email |
| `sent_at`         | DateTime   | UTC timestamp when sent (nullable)                                             |

---

### `smtp_credentials`

SMTP sender accounts used by the dispatcher.

| Column             | Type       | Notes                                                                                                                                        |
|--------------------|------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| `id`               | Integer PK | Auto-increment                                                                                                                               |
| `host`             | String     | SMTP server hostname                                                                                                                         |
| `port`             | Integer    | 465 (TLS) or 587 (STARTTLS)                                                                                                                  |
| `username`         | String     | Email address                                                                                                                                |
| `password`         | String     | Plain-text app password                                                                                                                      |
| `is_active`        | Boolean    | False = permanently disabled (auth failure)                                                                                                  |
| `cooldown_until`   | DateTime   | Null = available; future timestamp = temporarily paused                                                                                      |
| `daily_send_limit` | Integer    | Nullable; `None` = unlimited. Once today's sent count reaches this, the credential is excluded from the dispatch pool until the next UTC day |

**Note:** Passwords are stored plain-text. Ensure the database file has appropriate filesystem permissions.

---

### `blacklist`

Emails/domains blocked from receiving campaign emails.

| Column   | Type       | Notes                               |
|----------|------------|-------------------------------------|
| `id`     | Integer PK | Auto-increment                      |
| `email`  | String     | Lowercase, unique, indexed          |
| `domain` | String     | Indexed (auto-extracted from email) |
| `reason` | String     | Nullable; set on hard bounce        |

Entries are added automatically on SMTP hard bounces (codes 550, 553) and manually via `POST /api/blacklist`.

---

### `test_campaigns`

Test email campaigns using dummy recipient data.

| Column               | Type       | Notes                                                |
|----------------------|------------|------------------------------------------------------|
| `id`                 | Integer PK | Auto-increment                                       |
| `name`               | String     | Display name                                         |
| `template_id`        | Integer FK | → `email_templates.id` (nullable)                    |
| `test_credential_id` | Integer FK | → `smtp_credentials.id` (nullable)                   |
| `status`             | Enum       | `CampaignStatus`                                     |
| `pause_reason`       | String     | Nullable; same semantics as `campaigns.pause_reason` |
| `created_at`         | DateTime   | UTC                                                  |

---

### `test_campaign_emails`

Individual staged emails for a test campaign.

| Column             | Type       | Notes                                                                          |
|--------------------|------------|--------------------------------------------------------------------------------|
| `id`               | Integer PK | Auto-increment                                                                 |
| `test_campaign_id` | Integer FK | → `test_campaigns.id`                                                          |
| `recipient_email`  | String     | Dummy recipient address                                                        |
| `subject`          | String     | Rendered from template                                                         |
| `body`             | Text       | Rendered from template                                                         |
| `status`           | Enum       | `EmailStatus`                                                                  |
| `is_selected`      | Boolean    | Default true                                                                   |
| `missing_fields`   | String     | Nullable                                                                       |
| `error_message`    | String     | Nullable                                                                       |
| `credential_id`    | Integer FK | → `smtp_credentials.id` (nullable); which credential sent/attempted this email |
| `sent_at`          | DateTime   | Nullable                                                                       |

---

## Entity Relationship Summary

```
domains ── (domain_ids JSON selection record) ──▶ crawl_jobs
    │                                                 │ (create_job snapshots each seed)
    │ (copied at crawl time)                          ▼
    └──────────────────────────────────────▶ crawl_snapshots ──┐
                                                    ▲           │ (snapshot_id FK — source of
crawl_jobs ──────┬────────────────┐   leads ───────┘           │  all domain-derived fields)
    │            │                │       │                     │
    │ (job_id)   │ (job_id)       │       │ (lead_id FK)        │
    ▼            ▼                │       ▼                     │
visited_urls  job_custom_urls     │  campaign_emails ─────┐     │
                                  │       │              │ (credential_id)
        (leads.domain_id → domains.id     ▼              │
         is a soft link only)         campaigns ── email_templates
                                       │      │          │
                      (campaign_credentials)  │          │
                                       │      ▼          ▼
                                       └─ smtp_credentials
                                              │
                                          blacklist
```

---

## Database Class — Key Methods

The `Database` class in `database.py` wraps all SQL access via its mixins. A new `Session` context is opened and closed
for each method call.

### Domain Methods — `portal/db/mixins/domain_mixin.py`

| Method                            | Description                                                                   |
|-----------------------------------|-------------------------------------------------------------------------------|
| `upsert_domain(...)`              | Insert or update by `external_id` (fallback `main_url`); returns `id`         |
| `update_domain_url(id, url, ...)` | Manually set `main_url`/`contact_url` on a no-URL domain                      |
| `clear_domains()`                 | Delete all domain rows                                                        |
| `count_domains()`                 | Total domain count                                                            |
| `get_domain_stats(...)`           | `{total, crawlable, not_crawlable, duplicate}`, same filters as `get_domains` |
| `get_categories()`                | `[{code, title, count}]` grouped                                              |
| `get_states(category)`            | Distinct state list                                                           |
| `get_org_types(category, state)`  | `[{code, title, count}]` grouped                                              |
| `get_domains(...)`                | Paginated `(list[dict], total)`                                               |
| `get_domain_ids(...)`             | Matching, crawlable (`main_url` set) IDs for "select all"                     |
| `get_domains_by_ids(ids)`         | Fetch specific rows by PK list                                                |

### Job Methods — `portal/db/mixins/job_mixin.py`

| Method                                    | Description                                          |
|-------------------------------------------|------------------------------------------------------|
| `create_job(domain_ids/custom_urls, ...)` | Insert CrawlJob (sets `source_type`), return `id`    |
| `start_job(job_id)`                       | Set status=running, started_at=now                   |
| `finish_job(job_id, status, error)`       | Set terminal status + finished_at                    |
| `increment_job_progress(...)`             | Atomic increment of `leads_found`, `crawled_domains` |
| `update_job_metrics(...)`                 | Overwrite queue/visit/skip/depth/worker counts       |
| `get_job(job_id)`                         | Single job dict                                      |
| `list_jobs(limit)`                        | Recent jobs descending                               |

### Lead Methods — `portal/db/mixins/lead_mixin.py`

| Method                                   | Description                                                                                                                                               |
|------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `get_lead_score_weights()`               | Returns the active `lead_score.weights` dict                                                                                                              |
| `save_lead(...)`                         | Insert with global email dedup; takes `snapshot_id`, freezes state/org_type + recovers `domain_id` from the snapshot; computes `lead_score`; returns bool |
| `get_leads(...)`                         | Paginated `(list[dict], total)` with join to `crawl_snapshots`                                                                                            |
| `get_lead_ids(...)`                      | All matching IDs                                                                                                                                          |
| `get_all_leads_for_export(...)`          | Full rows for CSV export                                                                                                                                  |
| `get_lead_categories(job_id)`            | Category counts for leads                                                                                                                                 |
| `get_lead_states(job_id, category)`      | Distinct states                                                                                                                                           |
| `get_lead_org_types(job_id)`             | Organization-type counts for leads (leads-scoped, unlike `/api/org-types`)                                                                                |
| `bulk_upsert_manual_leads(job_id, rows)` | CSV-import manual leads (`channel_tag="manual"`, score forced to 0)                                                                                       |
| `update_lead(lead_id, updates)`          | Edit name/designation/department/state; recomputes `lead_score`                                                                                           |

`get_leads`, `get_lead_ids`, and `get_all_leads_for_export` all build their filter set through a shared
`_apply_lead_filters()` static helper (`job_id`, `category`, `state`, `search`, `complete_only`, `min_score`,
`org_type`, `show_manual`, `require_name`, `require_designation`, `require_phone`), so pagination totals can never
diverge from the row query. `get_leads` additionally sorts through a separate `_apply_lead_sort()` helper
(`sort_by` ∈ `score`/`contact`/`name`, `sort_dir`) — sorting is deliberately kept out of the filter helper so the
two concerns don't tangle. `category`/`state`/`org_type` filters all read from the joined `crawl_snapshots` row (
previously `category`/`state`
came from a live `domains` join and `org_type` from the frozen `Lead` column — now unified). They are bypassed for
manual leads (which have no snapshot) when `show_manual` is true; `min_score` never excludes manual leads (they're
always 0 by design).

### Crawl Snapshot Methods — `portal/db/mixins/crawl_snapshot_mixin.py`

| Method                             | Description                                                                                   |
|------------------------------------|-----------------------------------------------------------------------------------------------|
| `create_crawl_snapshot(job_id, d)` | Get-or-insert a frozen seed snapshot on `(job_id, source_domain_id)`; returns snapshot id     |
| `get_crawl_snapshots(job_id)`      | All frozen seed snapshots for a job (raw rows: snapshot `id` + `source_domain_id` + metadata) |

`get_leads`, `get_lead_ids`, and `get_all_leads_for_export` all build their filter set through a shared
`_apply_lead_filters()` static helper, so pagination totals can never diverge from the row query (a duplicated
filter block previously made that possible).

### Visited URL Methods — `portal/db/mixins/visited_mixin.py`

| Method                          | Description                              |
|---------------------------------|------------------------------------------|
| `mark_visited(url, job_id)`     | Insert with `IntegrityError` handling    |
| `get_visited_urls(job_id)`      | Set of URLs for this job                 |
| `get_recently_visited_global()` | URLs from all jobs within `recrawl_days` |
| `clear_visited_urls()`          | Truncate table                           |

### Campaign / Email Methods — `portal/db/mixins/outreach_mixin.py`

See that file for the full set of template, blacklist, campaign, credential, and test campaign methods (~35 methods,
the largest mixin).
