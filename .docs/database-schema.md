# Database Schema

Two databases (see [architecture.md](architecture.md#the-two-databases)):

- **Cloud** — the database of record. SQLAlchemy ORM (`cloud/db/`), Postgres in production or SQLite in
  desktop/dev, selected by `database.uri`. Access is through `cloud.db.Database` only; no raw sessions
  elsewhere. Migrations are Alembic (`alembic/versions/`).
- **Local** — two per-machine `sqlite3` stores, neither part of the shared schema nor migrated by Alembic:
  `agent/local_store.py` (the per-job durable outbox + frontier checkpoint) and `agent/localdb.py` (this
  machine's own settings — `cloud_api_base_url`, a durable `agent_id` — plus its visited-URL recrawl
  history). As of plan.md §19.1 Phase 9 Part 2, **nothing in either local store is ever synced to the
  cloud** — leads are the only thing that flows outward.

Status/kind fields are stored as **`TEXT`** (or SQLAlchemy `Enum`, which is `VARCHAR`+CHECK) rather than
native Postgres `ENUM`, so future migrations stay cheap — `ALTER TYPE` is transaction-hostile and can't drop
values.

---

## Enumerations (`shared/enums.py`)

| Enum             | Values                                                                              | Notes                                                                                                              |
|------------------|-------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `JobStatus`      | `pending`, `running`, `done`, `failed`, `cancelled`, `interrupted`, `manual_upload` | **lowercase** values; `interrupted` = reaped/resumable; `manual_upload` = synthetic job holding CSV-imported leads |
| `CampaignStatus` | `RUNNING`, `PAUSED`, `CANCELLED`, `COMPLETED`                                       | **uppercase** values (mind the case difference vs `JobStatus` when filtering)                                      |
| `CampaignKind`   | `production`, `test`                                                                | discriminator that unified the old `test_campaigns` table into `campaigns`                                         |
| `EmailStatus`    | `DRAFT`, `QUEUED`, `SENDING`, `SENT`, `FAILED`                                      | `SENDING` = at-most-once claim held during the SMTP call                                                           |

---

## Cloud tables

### Auth / RBAC / audit — `cloud/db/tables/auth.py`

**`users`** — `id` PK · `email` (unique, indexed, lowercased) · `password_hash` (argon2id) ·
`full_name` · `is_active` (revoke flips this) · `is_admin` (short-circuits every permission check) ·
`role_id` → `roles.id` · `token_version` (bump to invalidate live JWTs) · `failed_logins` ·
`locked_until` · `last_login_at` · `created_by` → `users.id` · `created_at` · `updated_at`.

**`roles`** — `id` PK · `name` (unique) · `description` · `is_system` (protect built-ins).
**`permissions`** — `key` PK · `description`.
**`role_permissions`** — `role_id` → `roles.id` (CASCADE) · `permission_key` → `permissions.key`;
`UNIQUE(role_id, permission_key)`.
**`user_permissions`** — per-user override: `user_id` · `permission_key` · `effect` (`grant`|`deny`);
`UNIQUE(user_id, permission_key)`.
**`user_sessions`** — refresh-token store: `id` · `user_id` · `refresh_token_hash` (sha256) · `user_agent` · `ip` ·
`created_at` · `last_used_at` · `expires_at` · `revoked_at`.
**`audit_log`** — append-only: `id` · `user_id` (null = system) · `action` · `target_type` · `target_id` · `detail` (
JSON) · `ip` · `created_at` (indexed). The runtime DB role has no `UPDATE`/`DELETE` on this table (Alembic 0020).

See [authentication.md](authentication.md) for the permission catalog and role defaults.

### Catalog & lookups — `cloud/db/tables/crawl.py`, `lookups.py`

**`categories`** (`code` PK · `title`) and **`org_types`** (`code` PK · `title`) — code→title lookups
that kill the old per-row title denormalization.

**`domains`** — `id` PK · `category_code` (indexed) · `category_title` · `state` (indexed) · `org_type`
(indexed) · `org_type_title` · `title` (indexed) · `main_url` (nullable = "not crawlable") · `contact_url`
· `external_id` (india.gov.in `npi_sanitized_id`, indexed) · `imported_at`. Re-imports destructively
rebuild this table — which is exactly why leads read snapshots, not `domains`.

### Crawl jobs — `cloud/db/tables/crawl.py`

**`crawl_jobs`** — `id` PK · `category_filter` · `title_filter` · `domain_ids` (legacy JSON, superseded by
the junction) · `source_type` (`domains`|`custom_urls`) · `status` · `owner_id` → `users.id` ·
`agent_hostname` · `last_heartbeat_at` (stale → reaped to `interrupted`) · `cancel_requested` ·
`total_domains`/`seed_domains` · live-metrics cache (`crawled_domains`, `queued_urls`, `visited_urls`,
`skipped_urls`, `leads_found`, `current_depth`, `active_workers`) · `error_message` · `created_at` ·
`started_at` · `finished_at`. The metrics columns are a deliberate materialized cache for the dashboard poll.

**`crawl_job_domains`** — junction (`job_id` CASCADE, `domain_id`) replacing the JSON `domain_ids` array.
**`job_custom_urls`** — `id` · `job_id` (CASCADE) · `url` · `created_at`; `UNIQUE(job_id, url)`.
**`crawl_snapshots`** — a **deliberate** frozen point-in-time copy of a seed's metadata so leads survive
catalog rebuilds: `id` · `job_id` (CASCADE) · `source_domain_id` (soft link) · `external_id` ·
`category_code`/`category_title` · `state` · `org_type`/`org_type_title` · `title` · `main_url` ·
`contact_url` · `created_at`; `UNIQUE(job_id, source_domain_id)` for get-or-insert.

`agent_hostname` on `crawl_jobs` is stamped with the creating agent's durable `agent_id` at `POST /jobs`
and used to reject a resume from any other agent (plan.md §19.1 Phase 9 Part 2, 2.5) — the frontier and
visited-URL data a resume needs live only on that one machine. There is no `job_frontiers`/`visited_urls`
table anymore (dropped by `alembic/versions/0023_drop_visited_and_frontier.py`) — both went 100% agent-local.

**`app_settings`** — generic key/value store (plan.md §19.1 Phase 8): `key` PK · `value` (JSON) ·
`updated_by` → `users.id` (nullable — null for the first-run seed write) · `updated_at`. One row this
phase, `key='crawl_policy'`, holding the extraction/lead-score/crawl-filter values that must be identical
across every crawler — see [configuration.md](configuration.md).

### Leads (shared pool) — `cloud/db/tables/leads.py`

**`leads`** — `id` PK · `job_id` (first-capturing job) · `snapshot_id` → `crawl_snapshots.id` (source of
domain-derived display fields) · `email` (indexed) · `person_name` · `designation` · `department` ·
`source_url` · `source_title` · `context_snippet` · `manual_state` (editable **only** for manual/CSV leads;
crawled leads read state from the snapshot) · `entity_kind` · `phone` · `channel_tag` (`manual` for CSV,
else extraction-set) · `confidence_band` (`HIGH`/`LOW`) · `field_provenance` (JSON) · `lead_score` (0–100)
· `depth` · `captured_at`; **`UNIQUE(job_id, email)`**.

> `save_lead` uses global email dedup with **enrich-on-conflict**: an existing lead's null fields are
> filled from a later finder, rather than discarding the second finder's data. See
> `cloud/db/mixins/lead_mixin.py`.
>
> **Fixed (issue #58, WI-9 Bug B):** `confidence_band`/`field_provenance` are the one exception to
> null-fill — they **upgrade-if-better** instead. A later capture whose band outranks the stored one
> (`HIGH` > `LOW`) replaces it and `lead_score` is recomputed; a worse band never downgrades it. So a
> mailto/microdata re-capture of an email first scraped from page text is correctly promoted to `HIGH`.

**`lead_occurrences`** — every capture of a shared lead (many-to-many), so per-job attribution and truthful
per-job `leads_found` survive dedup: `id` · `lead_id` (CASCADE) · `job_id` (CASCADE) · `captured_by` →
`users.id` · `source_url` · `captured_at`; `UNIQUE(lead_id, job_id)`.

### Outreach — `cloud/db/tables/outreach.py`

**`email_templates`** — `id` · `name` · `subject` (Jinja2) · `raw_body` (Jinja2).
**`campaigns`** — `id` · `name` · `template_id` · `kind` (`production`|`test`) · `test_credential_id`
(kind='test' only) · `status` (`CampaignStatus`) · `owner_id` · `pause_reason` · `created_at`.
**`campaign_emails`** — `id` · `campaign_id` (CASCADE) · `lead_id` (null for test/dummy) ·
`recipient_email` · `subject`/`body` (rendered) · `status` (`EmailStatus`) · `is_selected` ·
`missing_fields` · `error_message` · `credential_id` · `sending_since` · `sent_at`.
**`campaign_credentials`** — junction (`campaign_id` CASCADE, `credential_id`); empty = any active credential.
**`smtp_credentials`** — `id` · `host` · `port` · `username` · `password_encrypted` (Fernet `BYTEA`, **never
plaintext**) · `is_active` · `cooldown_until` · `daily_send_limit` (null = unlimited); `UNIQUE(host, username)`.
**`blacklist`** — `id` · `email` (unique) · `domain` (indexed) · `reason`.

---

## Local stores (agent, per machine)

### `agent/local_store.py` (`LocalOutbox`) — per-job durable outbox + frontier

Plain `sqlite3`, `PRAGMA synchronous=FULL` (a queued row survives power loss, not just a clean crash), one
`threading.Lock` guarding every method. Three tables:

**`outbox`** — `id` PK · `job_id` · `kind` (`lead` — the only kind since visited URLs stopped being
outboxed, plan.md §19.1 Phase 9 Part 2) · `payload_json` · `created_at` · `attempts` · `last_error`; index
`(job_id, kind)`. The write-ahead buffer drained by the flusher.
**`outbox_dead`** — dead-letter: rows that exceed `MAX_ATTEMPTS` (8) move here (`died_at`) so one poison
record can't block the queue.
**`frontier`** — `job_id` PK · `snapshot_json` · `saved_at`; one upserted row per job, the resume checkpoint
— always local-only now, no cloud counterpart exists to fall back to.

### `agent/localdb.py` — this machine's settings + visited history

Plain `sqlite3` too, `PRAGMA user_version` step-runner instead of Alembic. Two tables:

**`local_settings`** — `key` PK · `value`; holds `cloud_api_base_url` (set via the launcher's first-run
prompt) and a durable `agent_id` (a UUID minted once, never a real hostname — this is what gets stamped
onto `crawl_jobs.agent_hostname`).
**`visited_history`** — `url` PK · `visited_at`; this agent's own cross-job recrawl-protection history,
consulted at job create/resume (`agent/api.py:_local_visited_bootstrap`) instead of asking the cloud. Never
synced anywhere.

Session state itself (refresh token, last email) still lives in the **OS keyring**, written by
`agent/launcher/app.py` / `agent/identity.py`.

---

## Entity relationships (cloud)

```
users ──< crawl_jobs ──< crawl_job_domains >── domains
  │           │      ├──< job_custom_urls
  │           │      └──< crawl_snapshots ──< leads ──< lead_occurrences >── (job, user)
  ├──< campaigns ──< campaign_emails >── leads
  │        └──< campaign_credentials >── smtp_credentials
  ├──< user_sessions          roles ──< role_permissions >── permissions
  ├──< user_permissions >── permissions
  └──< audit_log
categories / org_types  ── code→title lookups referenced by domains & snapshots
blacklist  ── standalone (email/domain suppression)
app_settings  ── standalone key/value store; updated_by soft-references users.id
```

---

## `Database` — composition & key methods

`Database` (`cloud/db/database.py`) is composed from seven mixins (`VisitedUrlMixin` was retired in plan.md
§19.1 Phase 9 Part 2 — visited-URL tracking moved 100% agent-local). `__init__` creates the engine
(`pool_pre_ping=True`), runs `create_all()`, then `_seed_app_settings()` (first-run backfill of the
`crawl_policy` row from `config.yaml`) → `_ensure_columns()` → `run_migrations()` (Alembic
stamp-then-upgrade) → `seed_rbac()`. `_ensure_columns()` triggers `_recompute_lead_scores()` only once, the
moment `leads.lead_score` is newly added to an old DB (not every startup — Phase 8 replaced that with a
targeted trigger in `cloud/api/config.py`'s `POST` handler, fired only when a weight actually changes) —
plus the existing one-time `_backfill_snapshots()`.

| Mixin (`cloud/db/mixins/`) | Representative methods                                                                                                                                                                                                                                                                                                                                                             |
|----------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `app_settings_mixin`       | `get_app_setting`, `set_app_setting` (optimistic `updated_at` check), `get_crawl_policy`                                                                                                                                                                                                                                                                                           |
| `auth_mixin`               | `seed_rbac`, `create_user`, `get_user_by_email/id`, `set_password`, `set_user_active/role`, `record_login_success/failure`, `resolve_effective_permissions`, `create/rotate/revoke_session`, `revoke_session_family`, `write_audit`                                                                                                                                                |
| `domain_mixin`             | `upsert_category/org_type/domain`, `update_domain_url`, `clear_domains`, `get_domain_stats`, `get_categories/states/org_types`, `get_domains`, `get_domain_ids`, `get_domains_by_ids`                                                                                                                                                                                              |
| `job_mixin`                | `create_job` (stamps `agent_hostname`), `start_job`, `finish_job` (no-op if terminal), `heartbeat` (revives interrupted, returns cancel), `reap_stale_jobs`, `resume_job`, `claim_or_verify_job_agent` (resume ownership guard), `set_cancel_requested`, `get_or_create_manual_upload_job`, `get_running_jobs` (DB-backed, admin activity), `get_job`/`list_jobs` (owner-filtered) |
| `crawl_snapshot_mixin`     | `create_crawl_snapshot` (get-or-insert, race-safe), `get_crawl_snapshots`                                                                                                                                                                                                                                                                                                          |
| `lead_mixin`               | `save_lead` (enrich-on-conflict + occurrence + score), `bulk_save_leads`, `get_leads`, `get_lead_ids`, `get_all_leads_for_export`, `get_lead_categories/states/org_types`, `bulk_upsert_manual_leads`, `update_lead`                                                                                                                                                               |
| `outreach_mixin`           | templates/blacklist/credentials CRUD; campaigns; `claim_next_queued_email` (atomic QUEUED→SENDING), `recover_stuck_sending`, `mark_email_sent/failed`, `set_credential_cooldown`, `get_campaign_stats`, `get_credential_health`                                                                                                                                                    |

---

## Migration chain

`alembic/versions/`, head = **`0024_ddl_owned_ensure_columns`**. Linear except a branch at `0005` (`_add_lead_depth`
and `_add_lead_grading` both descend from `0004` and merge at `0006`). `alembic/env.py` targets
`cloud.db.Base` and honors the `DATABASE_URL` env var (so the Docker `migrate` service points at Postgres).

| Revision                           | Adds                                                                                                                                                                                                   |
|------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `0000`                             | core tables (domains, crawl_jobs, leads, visited_urls)                                                                                                                                                 |
| `0001`–`0011`                      | outreach models, test campaigns, email selection, lead depth/grading, custom URLs, `external_id`, campaign credentials, pause reason, lead score, crawl snapshots                                      |
| `0012_add_auth`                    | roles, permissions, users, role/user permissions, sessions, audit_log                                                                                                                                  |
| `0013_add_lookups_and_job_domains` | categories, org_types, `crawl_job_domains` (backfilled from JSON)                                                                                                                                      |
| `0014_add_ownership`               | `owner_id` on jobs/campaigns                                                                                                                                                                           |
| `0015_lead_occ_manual_state`       | `lead_occurrences`, `leads.manual_state`; drops vestigial `leads.domain_*`                                                                                                                             |
| `0016_merge_campaign_kind`         | `campaigns.kind`; folds `test_campaigns` in; drops the test tables                                                                                                                                     |
| `0017_encrypt_credentials`         | `smtp_credentials.password_encrypted` (Fernet)                                                                                                                                                         |
| `0018_job_resume_cancel`           | `cancel_requested`, `agent_hostname`, `last_heartbeat_at`                                                                                                                                              |
| `0019_add_sending_status`          | `EmailStatus.SENDING` + `campaign_emails.sending_since`                                                                                                                                                |
| `0020_least_privilege_role`        | Postgres-only `govcrawler_app` role, audit_log write-lockdown                                                                                                                                          |
| `0021_add_job_frontier`            | `job_frontiers` (cross-machine resume) — later dropped by `0023`                                                                                                                                       |
| `0022_add_app_settings`            | `app_settings` (DB-backed crawl policy, plan.md §19.1 Phase 8)                                                                                                                                         |
| `0023_drop_visited_and_frontier`   | Drops `visited_urls` + `job_frontiers` — both went 100% agent-local (plan.md §19.1 Phase 9 Part 2)                                                                                                     |
| `0024_ddl_owned_ensure_columns`    | `crawl_jobs.current_depth`/`active_workers`, `leads.snapshot_id` — moved from `_ensure_columns()` into Alembic so they're added by the DDL-owning `migrate` role, not the least-privilege runtime role |

Most other `_ensure_columns()` entries duplicate a column an earlier migration already adds — a defensive
no-op there for old SQLite installs where an Alembic ALTER-added column could silently not take effect
despite the migration being marked applied (see `0011`'s docstring). `lead_score` *values* (not the column,
added by `0010`) are backfilled by `_recompute_lead_scores()`, triggered once when the column is newly added
and again whenever `POST /api/config` changes a weight — desktop SQLite only, no DDL involved. The three
columns in `0024` were the only ones with **no** Alembic migration at all until `0024` added one: fine on
desktop SQLite (one connection, no role split), but fatal on Postgres once `0020`'s least-privilege
`govcrawler_app` role removed `api`/`dispatcher`'s ALTER TABLE rights — `_ensure_columns()` would try to add
them at app startup using that same restricted connection and fail with `InsufficientPrivilege`.
