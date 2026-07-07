# API Reference

Base URL: `http://127.0.0.1:8000`

All API responses are JSON unless noted. Pagination parameters use `page` (1-indexed) and `limit`.

---

## Frontend Pages

| Method | Path             | Description                          |
|--------|------------------|--------------------------------------|
| GET    | `/`              | Domains browser + crawl job creation |
| GET    | `/leads`         | Lead table with edit and export      |
| GET    | `/campaigns`     | Campaign management                  |
| GET    | `/settings`      | Crawler and extraction configuration |
| GET    | `/test-campaign` | Test campaign creation               |
| GET    | `/user-guide`    | In-app user guide                    |

---

## Metadata

### `GET /api/categories`

Returns all domain categories with domain counts.

**Response:**

```json
[
  { "code": "ug", "title": "Union Government", "count": 1247 },
  { "code": "sg", "title": "State / UT Government", "count": 834 }
]
```

---

### `GET /api/states`

Returns distinct states. Optionally filtered by category.

**Query params:** `category` (optional)

**Response:** `["Andhra Pradesh", "Bihar", ...]`

---

### `GET /api/org-types`

Returns organization types with counts. Optionally filtered.

**Query params:** `category` (optional), `state` (optional)

**Response:**

```json
[
  { "code": "dept", "title": "Departments", "count": 320 }
]
```

---

## Domains

### `GET /api/domains`

Paginated domain list with filters.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `category` | string | — | Filter by category code |
| `state` | string | — | Filter by state name |
| `org_type` | string | — | Filter by org type code |
| `search` | string | — | Search in title or URL |
| `sort_by` | string | — | Only `crawlable` is supported — groups domains with a `main_url` set ahead of (or behind) those without one |
| `sort_dir` | string | desc | `asc` or `desc` |
| `page` | int | 1 | Page number |
| `limit` | int | 50 | Max 200 |

**Response:**

```json
{
  "domains": [...],
  "total": 1234,
  "page": 1,
  "limit": 50,
  "pages": 25
}
```

Each domain object: `id, category_code, category_title, state, org_type, org_type_title, title, main_url, contact_url`

`main_url` (and `contact_url`) may be `null` — organizations imported from the india.gov.in directory with no
listed URL are kept rather than dropped, so metadata (title, category, state) isn't lost. The frontend marks these
"not crawlable" until a URL is added via `PATCH /api/domains/{id}`.

---

### `GET /api/domains/ids`

Returns matching, **crawlable** domain IDs (used by "Select All" in the UI). Domains with `main_url: null` are
excluded — they can't be used as crawl seeds, so select-all skips them.

**Query params:** same filters as `GET /api/domains` (no pagination)

**Response:** `{ "ids": [1, 2, 3, ...], "total": 150 }`

---

### `GET /api/domains/stats`

Total / crawlable / duplicate counts for domains matching the given filters — powers the stats strip above the
domain table.

**Query params:** same filters as `GET /api/domains` (no pagination). Omit all to get whole-table stats.

**Response:**

```json
{ "total": 1234, "crawlable": 1100, "not_crawlable": 134, "duplicate": 42 }
```

`duplicate` counts rows sharing a `main_url` with another row in the filtered set, minus one per group (the
redundant extra rows, not the whole group).

---

### `PATCH /api/domains/{id}`

Set or update a domain's crawlable URL — used to fix up organizations imported with `main_url: null`.

**Body:**

```json
{ "main_url": "https://example.gov.in", "contact_url": "https://example.gov.in/contact" }
```

`contact_url` is optional.

**Response:** the updated domain object. `404` if the domain doesn't exist, `422` if the URL is malformed.

---

## Configuration

### `GET /api/config`

Returns the current crawler and extraction settings as a flat object suitable for the settings form.

---

### `POST /api/config`

Saves updated settings to `portal/config.yaml`. New crawler settings take effect on the next job.

**Body:** flat JSON object with any subset of config fields (see [configuration.md](configuration.md) for keys).

**Response:** `{ "message": "Settings saved. Crawler settings take effect on the next job." }`

---

## Domain Import

### `POST /api/import/json`

Upload a `gov_domains.json` file. Zero API calls to india.gov.in. Runs in background. If an import is already
running, short-circuits with `{ "message": "Import already running", "status": <import_status> }` instead of
starting a second one.

**Body:** `multipart/form-data` with field `file` (JSON file).

**Response:** `{ "message": "JSON import started from <filename>" }`

---

### `POST /api/import`

Trigger a live refresh from the india.gov.in Web Directory API. Use only to update existing data. Same
already-running short-circuit as `POST /api/import/json`.

**Response:** `{ "message": "API import started" }`

---

### `GET /api/import/status`

Poll import progress.

**Response:**

```json
{
  "running": true,
  "source": "json",
  "total_categories": 7,
  "done_categories": 3,
  "total_entries": 1500,
  "inserted": 620,
  "error": null
}
```

---

## Crawl Jobs

### `POST /api/jobs`

Create and immediately start a crawl job. Seed with **either** known `domain_ids` **or** ad-hoc `custom_urls` —
exactly one must be provided (a request with both, or neither, gets a `422`).

**Body (domain-based):**

```json
{
  "domain_ids": [1, 2, 3],
  "category_filter": "ug",
  "title_filter": null
}
```

**Body (custom-URL-based):**

```json
{
  "custom_urls": ["https://example.gov.in", "another.gov.in/contact"],
  "category_filter": null,
  "title_filter": null
}
```

For `custom_urls`: each URL is trimmed, auto-prefixed with `http://` if it has no scheme, and deduplicated. The
`crawler.target_suffixes` restriction (e.g. `.gov.in`, `.nic.in`) is **not** applied — custom URLs are crawled as
given, since the caller chose them deliberately. Validation errors (`422`):

- `"Invalid URL(s): ..."` — one or more entries have no resolvable netloc.
- `"No valid custom URLs provided"` — nothing left after filtering blanks/invalid entries.
- `"Too many custom URLs (N); max is {max}"` — exceeds `crawler.max_custom_urls` (default 50).

For `domain_ids`: `404` if none of the IDs match an existing domain; `422 "Selected domains have no crawlable
URLs"` if every matched domain has `main_url: null`.

**Response:** `{ "id": 42, "message": "Crawl started for 3 seed URL(s)" }`

---

### `GET /api/jobs`

List recent crawl jobs.

**Query params:** `limit` (default 20, max 100)

**Response:** Array of job objects.

---

### `GET /api/jobs/{job_id}`

Get a single job's status and metrics. `404` if the job doesn't exist. `status` is dynamically overridden to
`"running"` if the job has a live, not-yet-done in-process task, even if the DB row hasn't caught up yet.

**Response:**

```json
{
  "id": 42,
  "status": "running",
  "total_domains": 10,
  "crawled_domains": 3,
  "seed_domains": 10,
  "queued_urls": 145,
  "visited_urls": 78,
  "skipped_urls": 22,
  "leads_found": 14,
  "current_depth": 2,
  "active_workers": 8,
  "error_message": null,
  "created_at": "2024-01-01T10:00:00",
  "started_at": "2024-01-01T10:00:01",
  "finished_at": null
}
```

---

### `GET /api/jobs/{job_id}/seeds`

Resolves a job's seeds. `404 "Job not found"` if it doesn't exist. The response shape depends on `source_type`:

- **Domain-based job:** array of domain objects (same shape as `GET /api/domains` rows).
- **Custom-URL job (`source_type == "custom_urls"`):** array of raw custom-URL records from `job_custom_urls`
  (`{id, job_id, url, created_at}`), **not** domain objects.

---

### `POST /api/jobs/{job_id}/cancel`

Cancel a running job. Marks status as `cancelled`.

**Response:** `{ "message": "Job cancelled" }`, or `{ "message": "Job is not currently running" }` if it wasn't
actively running when called.

---

## Leads

### `GET /api/leads`

Paginated leads with filters and sorting.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `job_id` | int, repeatable | — | Filter to one or more jobs (multi-select — repeat the param, e.g. `?job_id=1&job_id=2`) |
| `category` | string, repeatable | — | Filter by domain category (manual leads always bypass this — see `entry_type`) |
| `state` | string, repeatable | — | Filter by domain state (manual leads always bypass this — see `entry_type`) |
| `org_type` | string, repeatable | — | Filter by domain org type (manual leads always bypass this — see `entry_type`) |
| `search` | string | — | Search email, name, designation, department |
| `complete_only` | bool | false | Only leads with name + designation + department filled |
| `min_score` | int | — | Minimum `lead_score` (0-100); never excludes manual leads (always score 0) |
| `entry_type` | string | both | `manual` (CSV-imported only), `extracted` (crawled only), or `both` |
| `require_name` | bool | false | Only leads with `person_name` set |
| `require_designation` | bool | false | Only leads with `designation` set |
| `require_phone` | bool | false | Only leads with `phone` set |
| `sort_by` | string | — | `score`, `contact` (has phone), or `name` (has `person_name`, ignores designation) |
| `sort_dir` | string | desc | `asc` or `desc` |
| `page` | int | 1 | Page number |
| `limit` | int | 100 | Max 500 |

**Response:**

```json
{
  "leads": [...],
  "total": 500,
  "page": 1,
  "pages": 5
}
```

Each lead:
`id, email, person_name, designation, department, source_url, source_title, context_snippet, domain_title, category_code, domain_state, domain_org_type, confidence_band, field_provenance, channel_tag, phone, lead_score, depth, captured_at`

---

### `GET /api/leads/ids`

All matching lead IDs (for bulk operations). Same filters as `GET /api/leads` (no sort/pagination params).

**Response:** `{ "ids": [...], "total": 500 }`

---

### `GET /api/leads/score-weights`

Returns the active lead-scoring point weights (from `lead_score.weights` config, or the built-in defaults). Powers
the frontend's score-breakdown tooltip.

**Response:** `{ "email_high": 20, "email_low": 10, "person_name": 40, "designation": 30, "phone": 10 }`

---

### `GET /api/leads/categories`

Lead counts grouped by category.

**Query params:** `job_id` (optional, repeatable)

---

### `GET /api/leads/states`

Distinct states with leads.

**Query params:** `job_id` (optional, repeatable), `category` (optional, repeatable)

---

### `GET /api/leads/org-types`

Organization-type counts for leads — like `GET /api/org-types` but scoped to leads that actually exist (inner-joined
to `domains`), not every domain in the directory.

**Query params:** `job_id` (optional, repeatable)

**Response:** `[{ "code": "dept", "title": "Departments", "count": 42 }]`

---

### `POST /api/leads/import-csv`

Bulk-create or update manual leads from an uploaded CSV. Existing manual leads (matched by email) are updated;
leads that already exist as crawled (non-manual) are left untouched and reported in `skipped`.

**Body:** `multipart/form-data` with field `file` (CSV with columns `name, email, designation, department, phone`).

**Response:**
`{ "imported": 12, "updated": 2, "skipped": [{ "row": 5, "email": "x@y.gov.in", "reason": "email already exists as a crawled lead" }] }`

---

### `GET /api/leads/import-csv/template`

Downloads a blank CSV template (`text/csv`) with the expected columns and one example row.

---

### `POST /api/leads/export`

Download a CSV file of selected leads. Accepts the same filters as `GET /api/leads` (sorting is irrelevant to
export and is not accepted here).

**Body:**

```json
{
  "job_ids": [42],
  "categories": null,
  "states": null,
  "search": null,
  "complete_only": false,
  "min_score": null,
  "org_types": null,
  "entry_type": "both",
  "require_name": false,
  "require_designation": false,
  "require_phone": false,
  "lead_ids": [1, 2, 3],
  "fields": ["email", "person_name", "designation", "phone", "source_url"]
}
```

`lead_ids` and `fields` are optional. `email` is always included. If `fields` is omitted, all fields are exported.
Note the plural, list-typed filter fields (`job_ids`, `categories`, `states`, `org_types`) — these mirror the
multi-select filters on `GET /api/leads` and are NOT the same shape as that endpoint's repeatable query params.

**Available fields:**
`email, person_name, designation, department, phone, domain_title, domain_state, domain_org_type, category_title, source_url, source_title, context_snippet, lead_score, depth, captured_at`

**Response:** `text/csv` download.

---

### `PUT /api/leads/{lead_id}`

Update editable fields of a lead. Recomputes `lead_score` after the edit.

**Body:**

```json
{
  "person_name": "Dr. Rajesh Kumar",
  "designation": "Secretary",
  "department": "Ministry of Finance",
  "domain_state": "Delhi"
}
```

All fields are optional. Blank strings are stored as `null`.

---

## System

### `GET /api/logs`

Returns the last 1000 lines of `portal/data/portal.log`.

**Response:** `{ "logs": "..." }`

---

### `DELETE /api/visited-urls`

Clears the `visited_urls` table. Useful before a fresh full crawl.

**Response:** `{ "message": "Visited URLs cleared." }`

---

### `GET /api/system/activity`

Live counts of everything currently running — crawl jobs, real campaigns, and test campaigns. Powers the desktop
Control Panel's activity indicator and its "is it safe to stop the server?" check; not used by the web frontend.
Crawl jobs and real campaigns come from live in-memory task state (exact); test campaigns are inferred from
`status == RUNNING` in the DB, since test-campaign dispatch has no task handle to check — this can lag if the
process was killed mid-dispatch in a previous run.

**Response:**

```json
{
  "crawl_jobs": [{ "id": 12, "label": "Job #12 (4/10 domains, 7 leads)" }],
  "campaigns": [{ "id": 3, "name": "Q2 Outreach" }],
  "test_campaigns": [{ "id": 7, "name": "SMTP Test" }],
  "total_active": 2
}
```

---

### `POST /api/system/cancel-all`

Cancels every currently active crawl job, campaign, and test campaign in one call. Crawl jobs stop promptly; campaign
dispatch loops only re-check their status once per send cycle, so they can take **up to ~90 seconds** to actually
stop after this call returns.

**Response:**

```json
{
  "crawl_jobs_cancelled": 1,
  "campaigns_cancelled": 1,
  "test_campaigns_cancelled": 0,
  "message": "Cancellation signalled. Campaign dispatch loops may take up to ~90s to actually stop."
}
```

---

## Email Templates

### `GET /api/templates`

List all templates.

---

### `GET /api/templates/{template_id}`

Get a single template.

**Response:** `{ "id", "name", "subject", "raw_body" }`

---

### `POST /api/templates`

Create a template. Subject and body are validated for Jinja2 syntax.

**Body:** `{ "name": "...", "subject": "Dear {{ name }}", "raw_body": "..." }`

**Response:** `{ "id": 1, "message": "Template created" }`

---

### `PUT /api/templates/{template_id}`

Update a template. Any field can be updated; Jinja2 fields are re-validated.

**Body:** `{ "name": null, "subject": "...", "raw_body": "..." }` (all optional)

---

### `DELETE /api/templates/{template_id}`

Delete a template.

---

## Email Blacklist

### `GET /api/blacklist`

Paginated blacklist.

**Query params:** `page`, `limit` (max 200)

---

### `POST /api/blacklist`

Manually blacklist an email. Domain is auto-extracted from the email address.

**Body:** `{ "email": "foo@example.gov.in", "reason": "opt-out" }`

**Status:** 201 Created, or 409 if already blacklisted.

---

### `DELETE /api/blacklist/{blacklist_id}`

Remove a blacklist entry.

---

## SMTP Credentials

### `GET /api/credentials`

List all credentials. Passwords are masked (`••••••••`).

---

### `POST /api/credentials`

Add a new SMTP credential.

**Body:**

```json
{
  "host": "smtp.gmail.com",
  "port": 587,
  "username": "sender@example.com",
  "password": "app-password",
  "daily_send_limit": null
}
```

`daily_send_limit` is optional; `null`/omitted = unlimited. Once a credential hits its limit for the day it's
excluded from dispatch until the next UTC day.

**Supported ports:** 465 (TLS), 587 (STARTTLS)

---

### `PUT /api/credentials/{credential_id}`

Update a credential. All fields optional: `host`, `port`, `username`, `password`, `is_active`, `daily_send_limit`.

---

### `DELETE /api/credentials/{credential_id}`

Delete a credential.

---

### `POST /api/credentials/{credential_id}/test`

Test SMTP connection and authentication. **Side effects on credential state:** success re-activates the credential
if it was disabled; any failure (auth or otherwise) sets `is_active = false`.

**Response:**

```json
{ "success": true, "message": "Connection successful" }
{ "success": false, "error": "Authentication failed: ..." }
```

---

## Campaigns

### `POST /api/campaigns`

Generate draft emails for a new campaign. Blacklisted leads are skipped. Missing template variables (`name`,
`designation`) are detected and the email is automatically deselected.

**Body:**

```json
{
  "name": "Q2 Outreach",
  "template_id": 1,
  "lead_ids": [10, 11, 12],
  "credential_ids": []
}
```

`credential_ids` is optional — restricts which SMTP credentials this campaign may dispatch through. Empty (default)
= any active credential. See [outreach.md](outreach.md#credential-assignment).

**Response:**

```json
{
  "campaign_id": 5,
  "total_staged": 11,
  "blacklisted_count": 1,
  "message": "Campaign 'Q2 Outreach' created with 11 draft emails"
}
```

---

### `GET /api/campaigns`

Paginated campaign list (optionally including test campaigns).

**Query params:** `page`, `limit`, `include_test` (default false)

Each campaign includes an embedded `stats` object.

---

### `GET /api/campaigns/{campaign_id}`

Campaign detail including live `stats` and `credential_ids` (its current SMTP credential assignment).

---

### `PUT /api/campaigns/{campaign_id}/credentials`

Change which SMTP credentials a campaign may dispatch through, at any time before it's CANCELLED/COMPLETED. The
dispatcher re-reads this assignment on every send, so an edit to a RUNNING campaign takes effect on its next send.

**Body:** `{ "credential_ids": [1, 2, 3] }`

**Response:** `{ "message": "Campaign credentials updated" }`, or `400` if the campaign is CANCELLED/COMPLETED.

---

### `PATCH /api/campaigns/{campaign_id}`

Update campaign status.

**Body:** `{ "status": "PAUSED" }` — one of `RUNNING, PAUSED, CANCELLED, COMPLETED`

---

### `GET /api/campaigns/{campaign_id}/stats`

Live email counts by status (for UI polling every 3 s).

**Response:**
`{ "draft": 8, "queued": 2, "sent": 5, "failed": 1, "skipped": 2, "total": 18, "campaign_status": "RUNNING", "pause_reason": null }`

`skipped` = deselected DRAFT emails; `draft` = selected drafts only. `pause_reason` is set when the dispatcher
auto-pauses the campaign (e.g. every usable credential is disabled, cooling down, or capped) and cleared on any
subsequent status change.

---

### `GET /api/campaigns/{campaign_id}/emails`

Paginated list of staged emails.

**Query params:** `status` (optional filter), `page`, `limit` (max 200)

---

### `PUT /api/campaigns/{campaign_id}/emails/{email_id}`

Manually override subject and body of a DRAFT email.

**Body:** `{ "subject": "...", "body": "..." }`

---

### `PATCH /api/campaigns/{campaign_id}/emails/{email_id}/selection`

Select or deselect a DRAFT or QUEUED email for the next dispatch. Deselecting a QUEUED email pulls it back to DRAFT.

**Body:** `{ "is_selected": false }`

---

### `PATCH /api/campaigns/{campaign_id}/emails/selection-all`

Select or deselect every DRAFT email in the campaign in one call, regardless of pagination.

**Body:** `{ "is_selected": false }`

**Response:** `{ "message": "Updated selection for 8 email(s)", "updated": 8 }`

---

### `DELETE /api/campaigns/{campaign_id}/emails/{email_id}`

Permanently remove a DRAFT email from the campaign.

---

### `POST /api/campaigns/{campaign_id}/emails`

Add more leads to an existing campaign (renders template for new lead_ids, skips duplicates and blacklisted).

**Body:** `{ "lead_ids": [20, 21] }`

---

### `POST /api/campaigns/{campaign_id}/dispatch`

Start the background SMTP dispatch worker for this campaign.

**Validation before starting:**

- `404` if the campaign doesn't exist.
- `400 "No selected draft or queued emails to dispatch..."` if there's nothing to send.
- `409 "Campaign is already running"` if a dispatch task is already active for it.
- `400` if no usable SMTP credential exists — the message differs depending on whether none exist/are active at all
  vs. every assigned credential is currently capped, cooling down, or disabled.

**Response:** `{ "message": "Dispatch started" }`

---

## Test Campaigns

Mirror structure of production campaigns but use dummy recipient data instead of real leads.

| Method | Path                                              | Description                                     |
|--------|---------------------------------------------------|-------------------------------------------------|
| POST   | `/api/test-campaigns/parse-csv`                   | Parse a CSV into `dummy_details` (no DB writes) |
| POST   | `/api/test-campaigns`                             | Create test campaign with dummy recipients      |
| POST   | `/api/test-campaigns/{id}/dispatch`               | Dispatch test emails                            |
| GET    | `/api/test-campaigns/{id}`                        | Campaign detail + stats                         |
| GET    | `/api/test-campaigns/{id}/stats`                  | Live stats (includes `pause_reason`)            |
| GET    | `/api/test-campaigns/{id}/emails`                 | Email list                                      |
| PUT    | `/api/test-campaigns/{id}/emails/{eid}`           | Edit subject/body                               |
| PATCH  | `/api/test-campaigns/{id}/emails/{eid}/selection` | Select/deselect                                 |
| PATCH  | `/api/test-campaigns/{id}/emails/selection-all`   | Select/deselect every DRAFT email               |
| DELETE | `/api/test-campaigns/{id}/emails/{eid}`           | Remove draft                                    |
| PATCH  | `/api/test-campaigns/{id}`                        | Update status                                   |

**`POST /api/test-campaigns/parse-csv` body:** `multipart/form-data` with field `file` (same CSV shape as
`POST /api/leads/import-csv`). **Response:**
`{ "dummy_details": [{name, designation, email, department}, ...], "skipped": [...] }`
— feed the result straight into `dummy_details` below without any DB round-trip.

**`POST /api/test-campaigns` body:**

```json
{
  "name": "SMTP Test",
  "template_id": 1,
  "test_credential_id": 2,
  "dummy_details": [
    {
      "name": "Dr. Test User",
      "designation": "Director",
      "email": "test@example.com",
      "department": "Test Dept"
    }
  ]
}
```

`test_credential_id` is optional; falls back to round-robin over active credentials.
