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

Upload a `gov_domains.json` file. Zero API calls to india.gov.in. Runs in background.

**Body:** `multipart/form-data` with field `file` (JSON file).

**Response:** `{ "message": "JSON import started from <filename>" }`

---

### `POST /api/import`

Trigger a live refresh from the india.gov.in Web Directory API. Use only to update existing data.

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

Create and immediately start a crawl job.

**Body:**

```json
{
  "domain_ids": [1, 2, 3],
  "category_filter": "ug",
  "title_filter": null
}
```

**Response:** `{ "id": 42, "message": "Crawl started for 3 domains" }`

---

### `GET /api/jobs`

List recent crawl jobs.

**Query params:** `limit` (default 20, max 100)

**Response:** Array of job objects.

---

### `GET /api/jobs/{job_id}`

Get a single job's status and metrics.

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

Returns the list of seed domain records for a job.

**Response:** Array of domain objects.

---

### `POST /api/jobs/{job_id}/cancel`

Cancel a running job. Marks status as `cancelled`.

**Response:** `{ "message": "Job cancelled" }`

---

## Leads

### `GET /api/leads`

Paginated leads with filters.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `job_id` | int | — | Filter to a specific job |
| `category` | string | — | Filter by domain category |
| `state` | string | — | Filter by domain state |
| `search` | string | — | Search email, name, designation, department |
| `complete_only` | bool | false | Only leads with name + designation + department filled |
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
`id, email, person_name, designation, department, source_url, source_title, context_snippet, domain_title, category_code, domain_state, domain_org_type, captured_at`

---

### `GET /api/leads/ids`

All matching lead IDs (for bulk operations). Same filters as `GET /api/leads`.

**Response:** `{ "ids": [...], "total": 500 }`

---

### `GET /api/leads/categories`

Lead counts grouped by category.

**Query params:** `job_id` (optional)

---

### `GET /api/leads/states`

Distinct states with leads.

**Query params:** `job_id` (optional), `category` (optional)

---

### `POST /api/leads/export`

Download a CSV file of selected leads.

**Body:**

```json
{
  "job_id": 42,
  "category": null,
  "state": null,
  "search": null,
  "complete_only": false,
  "lead_ids": [1, 2, 3],
  "fields": ["email", "person_name", "designation", "source_url"]
}
```

`lead_ids` and `fields` are optional. `email` is always included. If `fields` is omitted, all fields are exported.

**Available fields:**
`email, person_name, designation, department, domain_title, domain_state, domain_org_type, category_title, source_url, source_title, context_snippet, captured_at`

**Response:** `text/csv` download.

---

### `PUT /api/leads/{lead_id}`

Update editable fields of a lead.

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
  "password": "app-password"
}
```

**Supported ports:** 465 (TLS), 587 (STARTTLS)

---

### `PUT /api/credentials/{credential_id}`

Update a credential. All fields optional.

---

### `DELETE /api/credentials/{credential_id}`

Delete a credential.

---

### `POST /api/credentials/{credential_id}/test`

Test SMTP connection and authentication. Re-activates credential on success.

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
  "lead_ids": [10, 11, 12]
}
```

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

Campaign detail including live stats.

---

### `PATCH /api/campaigns/{campaign_id}`

Update campaign status.

**Body:** `{ "status": "PAUSED" }` — one of `RUNNING, PAUSED, CANCELLED, COMPLETED`

---

### `GET /api/campaigns/{campaign_id}/stats`

Live email counts by status (for UI polling every 3 s).

**Response:**
`{ "draft": 8, "queued": 2, "sent": 5, "failed": 1, "skipped": 2, "total": 18, "campaign_status": "RUNNING" }`

`skipped` = deselected DRAFT emails; `draft` = selected drafts only.

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

Select or deselect a DRAFT email for the next dispatch.

**Body:** `{ "is_selected": false }`

---

### `DELETE /api/campaigns/{campaign_id}/emails/{email_id}`

Permanently remove a DRAFT email from the campaign.

---

### `POST /api/campaigns/{campaign_id}/emails`

Add more leads to an existing campaign (renders template for new lead_ids, skips duplicates and blacklisted).

**Body:** `{ "lead_ids": [20, 21] }`

---

### `POST /api/campaigns/{campaign_id}/dispatch`

Start the background SMTP dispatch worker for this campaign. Requires at least one active, non-cooling SMTP credential.

**Response:** `{ "message": "Dispatch started" }`

---

## Test Campaigns

Mirror structure of production campaigns but use dummy recipient data instead of real leads.

| Method | Path                                              | Description                                |
|--------|---------------------------------------------------|--------------------------------------------|
| POST   | `/api/test-campaigns`                             | Create test campaign with dummy recipients |
| POST   | `/api/test-campaigns/{id}/dispatch`               | Dispatch test emails                       |
| GET    | `/api/test-campaigns/{id}`                        | Campaign detail + stats                    |
| GET    | `/api/test-campaigns/{id}/stats`                  | Live stats                                 |
| GET    | `/api/test-campaigns/{id}/emails`                 | Email list                                 |
| PUT    | `/api/test-campaigns/{id}/emails/{eid}`           | Edit subject/body                          |
| PATCH  | `/api/test-campaigns/{id}/emails/{eid}/selection` | Select/deselect                            |
| DELETE | `/api/test-campaigns/{id}/emails/{eid}`           | Remove draft                               |
| PATCH  | `/api/test-campaigns/{id}`                        | Update status                              |

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
