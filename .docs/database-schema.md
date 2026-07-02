# Database Schema

Tables are defined as SQLAlchemy ORM models under [`portal/db/tables/`](../portal/db/tables/): `crawl.py` (Domain,
CrawlJob, VisitedUrl), `leads.py` (Lead), and `outreach.py` (Campaign, CampaignEmail, EmailTemplate, SMTPCredential,
Blacklist, TestCampaign, TestCampaignEmail). Enums live in [`portal/db/enums.py`](../portal/db/enums.py). The
`Database` wrapper class in [`portal/db/database.py`](../portal/db/database.py) provides all data-access methods —
its ~50 methods are composed from mixins under [`portal/db/mixins/`](../portal/db/mixins/), grouped by concern
(domain, job, lead, visited-url, outreach). Direct session usage elsewhere in the codebase is intentionally avoided.

**Backend:** SQLite (default, WAL mode) or PostgreSQL (set `database.uri` in config).

**Schema management:** SQLAlchemy `Base.metadata.create_all()` creates missing tables on startup. Incremental column
additions are handled by `Database._ensure_columns()`. Formal migrations are in `alembic/versions/`.

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

| Column           | Type       | Notes                                    |
|------------------|------------|------------------------------------------|
| `id`             | Integer PK | Auto-increment                           |
| `category_code`  | String     | e.g. `ug`, `sg`, `dist` — indexed        |
| `category_title` | String     | Human-readable e.g. "Union Government"   |
| `state`          | String     | State/UT name — indexed                  |
| `org_type`       | String     | Organization type code — indexed         |
| `org_type_title` | String     | e.g. "Departments", "Statutory Bodies"   |
| `title`          | String     | Organization name — indexed              |
| `main_url`       | String     | Root URL (scheme + netloc only)          |
| `contact_url`    | String     | Direct contact/directory page (nullable) |
| `imported_at`    | DateTime   | UTC timestamp of last import             |

**Upsert key:** `main_url` (existing rows are updated, not re-inserted).

---

### `crawl_jobs`

Tracks a single crawl run over a set of domains.

| Column            | Type       | Notes                                               |
|-------------------|------------|-----------------------------------------------------|
| `id`              | Integer PK | Auto-increment                                      |
| `domain_ids`      | Text       | JSON-serialized `list[int]` of seed domain IDs      |
| `category_filter` | String     | Optional filter label (metadata only)               |
| `title_filter`    | String     | Optional filter label (metadata only)               |
| `status`          | String     | `pending`, `running`, `done`, `failed`, `cancelled` |
| `total_domains`   | Integer    | Count of seed domains                               |
| `crawled_domains` | Integer    | Seed domains whose seed page was visited            |
| `seed_domains`    | Integer    | Same as `total_domains` (kept for display)          |
| `queued_urls`     | Integer    | URLs currently in the priority queue                |
| `visited_urls`    | Integer    | URLs visited in this session                        |
| `skipped_urls`    | Integer    | URLs skipped (recrawl, extension, non-gov, limit)   |
| `leads_found`     | Integer    | Total leads extracted                               |
| `current_depth`   | Integer    | Deepest crawl depth reached so far                  |
| `active_workers`  | Integer    | Workers currently processing a URL                  |
| `error_message`   | String     | Set on `failed` status                              |
| `created_at`      | DateTime   | UTC                                                 |
| `started_at`      | DateTime   | Set when status → `running`                         |
| `finished_at`     | DateTime   | Set when status → `done`/`failed`/`cancelled`       |

---

### `leads`

Extracted contact records.

| Column            | Type       | Notes                                   |
|-------------------|------------|-----------------------------------------|
| `id`              | Integer PK | Auto-increment                          |
| `job_id`          | Integer FK | → `crawl_jobs.id` — indexed             |
| `domain_id`       | Integer FK | → `domains.id` (nullable)               |
| `email`           | String     | Lowercase, indexed                      |
| `person_name`     | String     | Extracted or user-edited (nullable)     |
| `designation`     | String     | e.g. "Secretary", "Director" (nullable) |
| `department`      | String     | e.g. "Ministry of Finance" (nullable)   |
| `source_url`      | String     | Page the lead was extracted from        |
| `source_title`    | String     | `<title>` of that page (nullable)       |
| `context_snippet` | Text       | ±200 chars around the email (nullable)  |
| `domain_state`    | String     | Denormalized from domain at insert time |
| `domain_org_type` | String     | Denormalized from domain at insert time |
| `captured_at`     | DateTime   | UTC                                     |

**Unique constraint:** `(job_id, email)` — an email is stored at most once per job. Global dedup (across jobs) is
enforced at the `email` level in `save_lead()`.

**Editable fields:** `person_name`, `designation`, `department`, `domain_state` (via `PUT /api/leads/{id}`).

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

| Column        | Type       | Notes                             |
|---------------|------------|-----------------------------------|
| `id`          | Integer PK | Auto-increment                    |
| `name`        | String     | Display name                      |
| `template_id` | Integer FK | → `email_templates.id` (nullable) |
| `status`      | Enum       | `CampaignStatus`                  |
| `created_at`  | DateTime   | UTC                               |

---

### `campaign_emails`

Individual staged email drafts for a production campaign.

| Column            | Type       | Notes                                                           |
|-------------------|------------|-----------------------------------------------------------------|
| `id`              | Integer PK | Auto-increment                                                  |
| `campaign_id`     | Integer FK | → `campaigns.id`                                                |
| `lead_id`         | Integer FK | → `leads.id`                                                    |
| `recipient_email` | String     | Lowercase email address                                         |
| `subject`         | String     | Rendered from template                                          |
| `body`            | Text       | Rendered from template                                          |
| `status`          | Enum       | `EmailStatus`                                                   |
| `is_selected`     | Boolean    | Default true; false = deselected (skipped on dispatch)          |
| `missing_fields`  | String     | Comma-separated missing template vars (e.g. `name,designation`) |
| `error_message`   | String     | Set on FAILED status (nullable)                                 |
| `sent_at`         | DateTime   | UTC timestamp when sent (nullable)                              |

---

### `smtp_credentials`

SMTP sender accounts used by the dispatcher.

| Column           | Type       | Notes                                                   |
|------------------|------------|---------------------------------------------------------|
| `id`             | Integer PK | Auto-increment                                          |
| `host`           | String     | SMTP server hostname                                    |
| `port`           | Integer    | 465 (TLS) or 587 (STARTTLS)                             |
| `username`       | String     | Email address                                           |
| `password`       | String     | Plain-text app password                                 |
| `is_active`      | Boolean    | False = permanently disabled (auth failure)             |
| `cooldown_until` | DateTime   | Null = available; future timestamp = temporarily paused |

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

| Column               | Type       | Notes                              |
|----------------------|------------|------------------------------------|
| `id`                 | Integer PK | Auto-increment                     |
| `name`               | String     | Display name                       |
| `template_id`        | Integer FK | → `email_templates.id` (nullable)  |
| `test_credential_id` | Integer FK | → `smtp_credentials.id` (nullable) |
| `status`             | Enum       | `CampaignStatus`                   |
| `created_at`         | DateTime   | UTC                                |

---

### `test_campaign_emails`

Individual staged emails for a test campaign.

| Column             | Type       | Notes                   |
|--------------------|------------|-------------------------|
| `id`               | Integer PK | Auto-increment          |
| `test_campaign_id` | Integer FK | → `test_campaigns.id`   |
| `recipient_email`  | String     | Dummy recipient address |
| `subject`          | String     | Rendered from template  |
| `body`             | Text       | Rendered from template  |
| `status`           | Enum       | `EmailStatus`           |
| `is_selected`      | Boolean    | Default true            |
| `missing_fields`   | String     | Nullable                |
| `error_message`    | String     | Nullable                |
| `sent_at`          | DateTime   | Nullable                |

---

## Entity Relationship Summary

```
domains ──────────────────────────────────┐
    │                                     │
    │ (domain_ids JSON)                   │
    ▼                                     ▼
crawl_jobs ──────┐              leads ────────────┐
                 │                                │
                 │ (job_id FK)                    │ (lead_id FK)
                 ▼                                ▼
          visited_urls                  campaign_emails
                                              │
                                              ▼
                                          campaigns ───── email_templates
                                                  │
                                          smtp_credentials ──── (cooldown)
                                                  │
                                              blacklist
```

---

## Database Class — Key Methods

The `Database` class in `database.py` wraps all SQL access via its mixins. A new `Session` context is opened and closed
for each method call.

### Domain Methods — `portal/db/mixins/domain_mixin.py`

| Method                           | Description                                  |
|----------------------------------|----------------------------------------------|
| `upsert_domain(...)`             | Insert or update by `main_url`; returns `id` |
| `clear_domains()`                | Delete all domain rows                       |
| `count_domains()`                | Total domain count                           |
| `get_categories()`               | `[{code, title, count}]` grouped             |
| `get_states(category)`           | Distinct state list                          |
| `get_org_types(category, state)` | `[{code, title, count}]` grouped             |
| `get_domains(...)`               | Paginated `(list[dict], total)`              |
| `get_domain_ids(...)`            | All matching IDs for "select all"            |
| `get_domains_by_ids(ids)`        | Fetch specific rows by PK list               |

### Job Methods — `portal/db/mixins/job_mixin.py`

| Method                              | Description                                          |
|-------------------------------------|------------------------------------------------------|
| `create_job(domain_ids, ...)`       | Insert CrawlJob, return `id`                         |
| `start_job(job_id)`                 | Set status=running, started_at=now                   |
| `finish_job(job_id, status, error)` | Set terminal status + finished_at                    |
| `increment_job_progress(...)`       | Atomic increment of `leads_found`, `crawled_domains` |
| `update_job_metrics(...)`           | Overwrite queue/visit/skip/depth/worker counts       |
| `get_job(job_id)`                   | Single job dict                                      |
| `list_jobs(limit)`                  | Recent jobs descending                               |

### Lead Methods — `portal/db/mixins/lead_mixin.py`

| Method                              | Description                                          |
|-------------------------------------|------------------------------------------------------|
| `save_lead(...)`                    | Insert with global email dedup; returns bool         |
| `get_leads(...)`                    | Paginated `(list[dict], total)` with join to domains |
| `get_lead_ids(...)`                 | All matching IDs                                     |
| `get_all_leads_for_export(...)`     | Full rows for CSV export                             |
| `get_lead_categories(job_id)`       | Category counts for leads                            |
| `get_lead_states(job_id, category)` | Distinct states                                      |
| `update_lead(lead_id, updates)`     | Edit name/designation/department/state               |

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
