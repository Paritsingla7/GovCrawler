# GovCrawler — Multi-User, Cloud-Split, RBAC Overhaul Plan
> **Status:** Finalized v2 (living document). The normalization fixes from the schema audit are **applied** — §4 is
> the normalized target schema, not a proposal. Core crawling / extraction / outreach *behavior* is preserved; the
> overhaul changes where data lives, who may touch it, and how it's deployed.
>
> Reading order: §1 decisions → §2 architecture → §4 schema are the spine. §18 lists the small set of choices still
> worth an explicit confirm.

---

## Table of Contents

1. [Decisions](#1-decisions)
2. [Architecture & trust model](#2-architecture--trust-model)
3. [Data placement (the two-database split + config split)](#3-data-placement-the-two-database-split--config-split)
4. [Cloud schema (normalized target)](#4-cloud-schema-normalized-target)
5. [RBAC model](#5-rbac-model)
6. [Authentication](#6-authentication)
7. [API design](#7-api-design)
8. [The local agent](#8-the-local-agent)
9. [Concurrency & write-race strategy](#9-concurrency--write-race-strategy)
10. [Failure, fallback & resume](#10-failure-fallback--resume)
11. [Caching / Redis](#11-caching--redis)
12. [Hosting & deployment (single VPS)](#12-hosting--deployment-single-vps)
13. [Security & standards alignment](#13-security--standards-alignment)
14. [Migrations (forward strategy + one-time data migration)](#14-migrations-forward-strategy--one-time-data-migration)
15. [Repository & module structure](#15-repository--module-structure)
16. [End-to-end workflows (DB · location · permission)](#16-end-to-end-workflows-db--location--permission)
17. [Edge cases & scalability](#17-edge-cases--scalability)
18. [Open decisions to confirm](#18-open-decisions-to-confirm)
19. [Phased roadmap](#19-phased-roadmap)
20. [What stays exactly the same](#20-what-stays-exactly-the-same)

---

## 1. Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Topology | **Hybrid** — the crawl engine runs on each user's machine; shared data lives in a cloud Postgres; a small local SQLite holds per-machine config + session + resilience buffers. |
| 2 | Auth | **Local accounts, admin-provisioned.** Email + password (argon2id); admin creates/revokes users and sets permissions. No external IdP. |
| 3 | Cloud host | **Single self-managed VPS**, Docker Compose (Postgres + API + reverse proxy). |
| 4 | Data scoping | **Admin sees everything.** Regular users see only **their own jobs and campaigns**. **Leads are one shared pool** visible to anyone with `leads.view`. |
| 5 | SMTP dispatch | **Centralized on the VPS** (single worker) — correct rate-limiting, always-on, no cross-machine coordination. |
| 6 | Schema | Rebuilt as a **normalized target** for Postgres and populated by a transforming migration — not ALTERed in place, not blindly rebuilt from zero (§4, §14). |

Items still worth an explicit confirm before build are in §18.

---

## 2. Architecture & trust model

### 2.1 Why the client never touches Postgres directly

There is **no authentication anywhere in the code today** — every FastAPI route is open
([portal/api/server.py:42-66](portal/api/server.py)), and the only secret stored is the SMTP password, in plaintext
([portal/db/tables/outreach.py:33](portal/db/tables/outreach.py)). If each local client held a Postgres connection
string, RBAC would be decorative: anyone running the app could bypass every permission with raw SQL, and Postgres
would have to be exposed to the internet. So **all shared-data access goes through an authenticated API**; Postgres
binds to `127.0.0.1` on the VPS and is reachable only by the API process. The API is the single trust boundary.

The "two databases" are still real — cloud Postgres (shared + admin) and local SQLite (per-machine) — the client just
reaches the cloud one *through the API*, not over a raw socket.

### 2.2 Target architecture

```
┌───────────────────────── Local machine (per user, x~6) ─────────────────────────┐
│  Desktop launcher (Tkinter)  -- control panel + login screen                     │
│        │ starts/stops                                                            │
│        ▼                                                                          │
│  Local FastAPI (127.0.0.1, loopback only)                                         │
│    - Serves the operator web UI (index/leads/campaigns/settings)                 │
│    - BFF: proxies data calls to the Cloud API with the user's token              │
│      (token stays server-side -> none in the browser, no CORS)                    │
│    - Owns the crawl engine + parser (RUN LOCALLY, internals unchanged)           │
│  Local SQLite  --  runtime config + auth session + outbox + frontier only        │
└──────────────────────────────────────┬───────────────────────────────────────────┘
                                        │  HTTPS + Bearer JWT (Python httpx)
                                        ▼
┌──────────────────────────────── VPS (cloud) ────────────────────────────────────┐
│  Caddy / nginx  -- TLS (Let's Encrypt); :443 the only public port                │
│        ▼                                                                          │
│  Cloud API (FastAPI + Gunicorn)      Admin web UI (user mgmt, audit, all-jobs)    │
│    - Auth, RBAC, ownership scoping, audit                                        │
│    - All shared-data endpoints + crawler-agent coordination + central import     │
│  Dispatcher worker (single instance) -- SMTP send + pacing                        │
│        ▼                                                                          │
│  Postgres (127.0.0.1) -- shared data + users/roles/permissions/audit             │
│  (Redis — not provisioned initially; §11)                                        │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Two UIs, one frontend codebase.** The **operator UI** is served by the local BFF (the operator also drives the local
crawler); the **admin UI** is served by the cloud (admins don't run a crawler). Both render the same
`frontend/` components; admin-only pages are permission-gated. The admin is **not** a separate app or repo — it's the
cloud tier's `api/admin/` routers + `frontend/templates/admin/` pages (§15).

---

## 3. Data placement (the two-database split + config split)

### 3.1 What lives where

| Data | Location |
|------|----------|
| users, roles, permissions, sessions, audit_log, app_settings | **Cloud Postgres** |
| categories, org_types, domains, crawl_jobs, crawl_job_domains, crawl_snapshots, job_custom_urls | **Cloud Postgres** |
| leads, lead_occurrences, visited_urls | **Cloud Postgres** (shared pool) |
| campaigns, campaign_emails, campaign_credentials, email_templates, blacklist, smtp_credentials | **Cloud Postgres** |
| Per-machine runtime config + auth session + outbox + frontier | **Local SQLite** |

All shared data is in one Postgres DB, so no cross-DB foreign keys exist; the only thing on the client is config +
transient resilience state, which has no FK into shared data.

### 3.2 Splitting `config.yaml`

Today's `config.yaml` mixes *policy* (affects lead quality for everyone) with *machine runtime* (affects one box's
speed). Split by meaning:

| Config | Goes to | Why |
|--------|---------|-----|
| `extraction.*`, `lead_score.weights`, `crawler` policy (`target_suffixes`, `priority_keywords`, `skip_extensions`, `pagination`, `js_indicators`, `max_links_per_page`, `max_depth`, `recrawl_days`, `request_delay`, `user_agent`, `max_custom_urls`) | **Cloud** `app_settings` | Must be identical across crawlers; scoring recomputed centrally. |
| `crawler.workers`, `httpx_first`, `playwright_fallback`, timeouts, `js_settle_time`, `api.host/port` | **Local SQLite** | Machine performance / bind only. |
| `cloud_api_base_url`, current user, refresh token | **Local SQLite** (token in OS keyring) | Session bootstrap. |
| `database.uri` | **Removed from client** | Client has no DB URI. |

Cloud policy is edited on the admin Settings page (`settings.manage`) and delivered to crawlers at job start —
replacing today's runtime `config.yaml` write ([portal/api/config.py](portal/api/config.py)).

---

## 4. Cloud schema (normalized target)

One Postgres DB. DDL is indicative (each becomes an Alembic revision). **Status fields use `TEXT` + `CHECK`, not
native PG `ENUM`** — `ALTER TYPE` is transaction-hostile and can't drop values, so text+check keeps future migrations
cheap (§14). Normalization decisions are noted inline.

### 4.1 Reference / lookup tables (kill title-denormalization)

```sql
-- Code→title lookups: titles were previously repeated on every domain row (3NF fix).
CREATE TABLE categories ( code TEXT PRIMARY KEY, title TEXT NOT NULL );
CREATE TABLE org_types  ( code TEXT PRIMARY KEY, title TEXT NOT NULL );
```

### 4.2 Domains & catalog

```sql
CREATE TABLE domains (
    id             SERIAL PRIMARY KEY,
    category_code  TEXT REFERENCES categories(code),
    org_type_code  TEXT REFERENCES org_types(code),
    state          TEXT,                       -- natural attribute; small fixed set, left as text
    title          TEXT,
    main_url       TEXT,                        -- nullable: "not crawlable" until a URL is added
    contact_url    TEXT,
    external_id    TEXT,                        -- india.gov.in npi_sanitized_id
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON domains (category_code);
CREATE INDEX ON domains (org_type_code);
CREATE INDEX ON domains (state);
CREATE INDEX ON domains (external_id);
-- Dropped vs. today: category_title, org_type_title (now via lookup join).
```

### 4.3 Crawl jobs

```sql
CREATE TABLE crawl_jobs (
    id                SERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL DEFAULT 'domains'
                          CHECK (source_type IN ('domains','custom_urls')),
    category_filter   TEXT,                     -- metadata label only
    title_filter      TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','running','done','failed','cancelled','interrupted')),
    owner_id          INTEGER REFERENCES users(id),
    agent_hostname    TEXT,                     -- which machine is running it
    last_heartbeat_at TIMESTAMPTZ,              -- stale => reaped to 'interrupted' (§10)
    cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    total_seeds       INTEGER,                  -- seed count (domains OR custom urls); set at creation
    -- live-metrics CACHE (derivable, materialized for the 2s dashboard poll — intentional denormalization):
    crawled_domains   INTEGER DEFAULT 0,
    queued_urls       INTEGER DEFAULT 0,
    visited_urls      INTEGER DEFAULT 0,
    skipped_urls      INTEGER DEFAULT 0,
    leads_found       INTEGER DEFAULT 0,
    current_depth     INTEGER DEFAULT 0,
    active_workers    INTEGER DEFAULT 0,
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);
CREATE INDEX ON crawl_jobs (owner_id);
-- Dropped vs. today: domain_ids JSON (→ crawl_job_domains, 1NF fix); seed_domains (was a literal dup of total_domains).

-- Junction replacing the JSON domain_ids array (1NF fix; enables "which jobs used domain X").
CREATE TABLE crawl_job_domains (
    job_id    INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    domain_id INTEGER REFERENCES domains(id),
    PRIMARY KEY (job_id, domain_id)
);

CREATE TABLE job_custom_urls (
    id         SERIAL PRIMARY KEY,
    job_id     INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, url)
);
```

### 4.4 Crawl snapshots (intentional temporal denormalization — kept)

`crawl_snapshots` is a **deliberate** frozen point-in-time copy of a seed's metadata so leads survive catalog
rebuilds. This is correct snapshotting, not a defect — kept as-is (title fields frozen on purpose).

```sql
CREATE TABLE crawl_snapshots (
    id               SERIAL PRIMARY KEY,
    job_id           INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    source_domain_id INTEGER,                   -- soft link to domains.id at crawl time (nullable)
    external_id      TEXT,
    category_code    TEXT,  category_title TEXT,   -- frozen copies (snapshot, not a lookup)
    state            TEXT,
    org_type_code    TEXT,  org_type_title TEXT,   -- frozen copies
    title            TEXT,  main_url TEXT,  contact_url TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, source_domain_id)             -- get-or-insert per (job, domain)
);
```

### 4.5 Leads (shared pool)

```sql
CREATE TABLE leads (
    id                SERIAL PRIMARY KEY,
    job_id            INTEGER REFERENCES crawl_jobs(id),         -- first-capturing job
    snapshot_id       INTEGER REFERENCES crawl_snapshots(id),    -- source of domain-derived display fields
    email             TEXT NOT NULL,
    person_name       TEXT,
    designation       TEXT,
    department        TEXT,
    source_url        TEXT,
    source_title      TEXT,
    context_snippet   TEXT,
    manual_state      TEXT,          -- used ONLY for snapshot-less (manual/CSV) leads; crawled leads read from snapshot
    entity_kind       TEXT,
    phone             TEXT,
    channel_tag       TEXT,          -- 'manual' for CSV imports, else extraction-set
    confidence_band   TEXT,
    field_provenance  JSONB,         -- schemaless annotation — kept as JSON by design
    lead_score        INTEGER NOT NULL DEFAULT 0,
    depth             INTEGER NOT NULL DEFAULT 0,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_captured_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at        TIMESTAMPTZ,
    CONSTRAINT leads_email_key UNIQUE (email)     -- global dedup; matches today's effective behavior
);
CREATE INDEX ON leads (job_id);
CREATE INDEX ON leads (snapshot_id);
-- Dropped vs. today: domain_id (vestigial, superseded by snapshot); domain_org_type; domain_state
--   (dual-source with the snapshot join) — consolidated to a single nullable manual_state for manual leads.
-- save_lead uses INSERT ... ON CONFLICT (email) DO UPDATE to ENRICH (COALESCE nulls, keep higher
--   confidence_band) rather than discard the second finder's data; first_captured_by/at stay fixed.

-- Every capture of a shared lead (many-to-many), so per-job attribution + truthful per-job leads_found survive dedup.
CREATE TABLE lead_occurrences (
    id          BIGSERIAL PRIMARY KEY,
    lead_id     INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    job_id      INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    captured_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source_url  TEXT,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lead_id, job_id)
);
CREATE INDEX ON lead_occurrences (job_id);

CREATE TABLE visited_urls (
    id         SERIAL PRIMARY KEY,
    url        TEXT NOT NULL,
    job_id     INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    visited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (url, job_id)
);
CREATE INDEX ON visited_urls (url);
-- Periodic prune of rows older than recrawl_days (they no longer protect anything) keeps this bounded (§17).
```

### 4.6 Outreach (production + test unified)

`test_campaigns`/`test_campaign_emails` are **merged** into `campaigns`/`campaign_emails` via a `kind` discriminator
(nullable `lead_id` for test/dummy rows). This removes a near-duplicate table pair and roughly halves the outreach
data layer.

```sql
CREATE TABLE email_templates (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    subject  TEXT NOT NULL,        -- Jinja2
    raw_body TEXT NOT NULL         -- Jinja2
);

CREATE TABLE campaigns (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'production'
                           CHECK (kind IN ('production','test')),
    template_id        INTEGER REFERENCES email_templates(id),
    status             TEXT NOT NULL DEFAULT 'paused'
                           CHECK (status IN ('running','paused','cancelled','completed')),
    pause_reason       TEXT,
    owner_id           INTEGER REFERENCES users(id),
    test_credential_id INTEGER REFERENCES smtp_credentials(id),  -- kind='test' only; else null
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON campaigns (owner_id);

CREATE TABLE campaign_emails (
    id             SERIAL PRIMARY KEY,
    campaign_id    INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id        INTEGER REFERENCES leads(id),      -- null for test/dummy recipients
    recipient_email TEXT NOT NULL,
    subject        TEXT NOT NULL,                     -- rendered
    body           TEXT NOT NULL,                     -- rendered (dummy details captured at render time)
    status         TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','queued','sending','sent','failed')),  -- 'sending' = at-most-once claim
    is_selected    BOOLEAN NOT NULL DEFAULT TRUE,
    missing_fields TEXT,
    error_message  TEXT,
    credential_id  INTEGER REFERENCES smtp_credentials(id),
    sent_at        TIMESTAMPTZ
);
CREATE INDEX ON campaign_emails (campaign_id);

CREATE TABLE campaign_credentials (              -- which credentials a campaign may use (empty = all active)
    campaign_id   INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    credential_id INTEGER REFERENCES smtp_credentials(id),
    PRIMARY KEY (campaign_id, credential_id)
);

CREATE TABLE smtp_credentials (
    id                 SERIAL PRIMARY KEY,
    host               TEXT NOT NULL,
    port               INTEGER NOT NULL,
    username           TEXT NOT NULL,
    password_encrypted BYTEA NOT NULL,           -- Fernet/libsodium; key from env (§13). No more plaintext.
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_until     TIMESTAMPTZ,
    last_sent_at       TIMESTAMPTZ,              -- pacing + dispatch recovery
    daily_send_limit   INTEGER,
    created_by         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (host, username)
);

CREATE TABLE blacklist (
    id     SERIAL PRIMARY KEY,
    email  TEXT UNIQUE NOT NULL,
    domain TEXT GENERATED ALWAYS AS (split_part(email, '@', 2)) STORED,  -- can't drift from email (3NF fix)
    reason TEXT
);
CREATE INDEX ON blacklist (domain);
```

### 4.7 Auth / RBAC / audit / settings

```sql
CREATE TABLE roles (
    id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
    is_system BOOLEAN NOT NULL DEFAULT FALSE               -- protect built-ins
);
CREATE TABLE permissions ( key TEXT PRIMARY KEY, description TEXT NOT NULL );

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         CITEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,                            -- argon2id
    full_name     TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,            -- "revoke" flips this
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    role_id       INTEGER REFERENCES roles(id) ON DELETE RESTRICT,
    token_version INTEGER NOT NULL DEFAULT 0,               -- bump to invalidate live tokens
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    created_by    INTEGER REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE role_permissions (
    role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    permission_key TEXT REFERENCES permissions(key),
    PRIMARY KEY (role_id, permission_key)
);
CREATE TABLE user_permissions (                             -- per-user grant/deny overrides
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    permission_key TEXT REFERENCES permissions(key),
    effect TEXT NOT NULL CHECK (effect IN ('grant','deny')),
    PRIMARY KEY (user_id, permission_key)
);

CREATE TABLE user_sessions (                                -- refresh tokens (real logout + revocation)
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_hash TEXT NOT NULL,                       -- sha256 of the opaque token
    user_agent TEXT, ip INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ
);
CREATE INDEX ON user_sessions (user_id);
CREATE INDEX ON user_sessions (refresh_token_hash);

CREATE TABLE audit_log (                                    -- append-only (§13)
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),                   -- null = system
    action TEXT NOT NULL, target_type TEXT, target_id TEXT,
    detail JSONB, ip INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (created_at DESC);
CREATE INDEX ON audit_log (user_id);
CREATE INDEX ON audit_log (action);

CREATE TABLE app_settings (                                 -- global crawl policy / extraction / weights
    key TEXT PRIMARY KEY, value JSONB NOT NULL,
    updated_by INTEGER REFERENCES users(id), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.8 What changed vs. today, and what was deliberately kept

- **Applied:** `crawl_job_domains` junction (was JSON `domain_ids`); `categories`/`org_types` lookups (was repeated
  titles); dropped `crawl_jobs.seed_domains` (duplicate of the seed count) and `leads.domain_id`
  (vestigial); consolidated `leads.domain_state`/`domain_org_type` → one nullable `manual_state`; `blacklist.domain`
  as a generated column; **unified** `test_campaigns`→`campaigns`(`kind`); enums as `TEXT`+`CHECK`; encrypted SMTP
  password; global `UNIQUE(email)` on leads with enrich-on-conflict; `lead_occurrences` for per-job attribution.
- **Kept intentionally:** `crawl_snapshots` (temporal snapshot, correct by design); the `crawl_jobs` live-metrics
  columns (a materialized cache for the 2 s poll — deriving them per poll would be worse); JSON `field_provenance` /
  `app_settings.value` (genuinely schemaless).

---

## 5. RBAC model

**Role defaults + per-user overrides** (a user has one role's bundle plus optional `grant`/`deny` per capability).

### 5.1 Permission catalog

| Key | Guards |
|-----|--------|
| `users.manage` / `roles.manage` / `audit.view` | User & role administration; read the audit log |
| `settings.manage` | Edit global crawl policy / extraction / score weights |
| `domains.view` / `domains.import` | Browse catalog / trigger a central import |
| `crawl.run` / `crawl.cancel_all` | Create+start jobs / cancel any user's job |
| `jobs.view_all` | See all users' jobs (else own only) |
| `leads.view` / `leads.edit` / `leads.export` / `leads.import` | Shared-pool lead operations |
| `campaigns.manage` / `campaigns.dispatch` / `campaigns.view_all` | Create/edit own / dispatch / see all |
| `templates.manage` / `credentials.manage` / `blacklist.manage` | Outreach configuration |

### 5.2 Built-in roles (admin can override per user)

| | Admin | Operator | Viewer |
|-|:-:|:-:|:-:|
| users/roles/audit/settings | ✔ | — | — |
| jobs.view_all / campaigns.view_all / crawl.cancel_all | ✔ | — | — |
| crawl.run / domains.import | ✔ | ✔ | — |
| domains.view / leads.view | ✔ | ✔ | ✔ |
| leads.edit/export/import / campaigns.* / templates/credentials/blacklist | ✔ | ✔ | — |

`is_admin` short-circuits every check. Per-user `deny` does not apply to an `is_admin` user (a limited "admin" is a
non-admin role with broad grants) — intentional.

### 5.3 Enforcement (server-side)

```python
def require(*needed):
    async def dep(user = Depends(get_current_user)):
        if not user.has_all(needed):              # is_admin => always true
            raise HTTPException(403)
        return user
    return dep

# ownership lives inside the handler, not a blanket dependency:
q = select(CrawlJob)
if not user.can("jobs.view_all"):
    q = q.where(CrawlJob.owner_id == user.id)
```

`get_current_user` decodes the JWT, checks `is_active` + `token_version`, resolves effective permissions (role +
grants − denies), caches them on the request. Every mutating endpoint writes an `audit_log` row. Leads carry **no**
owner filter (shared); the `job_id` filter on leads is restricted to the caller's own jobs unless `jobs.view_all`.

---

## 6. Authentication

- **Passwords:** argon2id (`argon2-cffi`); never logged; guidance per NIST 800-63B (length over composition rules).
- **Tokens:** short-lived **access JWT** (~15 min; claims `sub`, `tv`=token_version, `exp`) + rotating **refresh
  token** (~14 days), stored hashed in `user_sessions`. **Revocation** = `is_active=false` + bump `token_version`
  (worst-case exposure ≈ access-token TTL). `token_version` bumps only on revoke + password change; ordinary
  permission edits apply at the next refresh. Rotated-token replay revokes the whole session family.
- **Clients:** local app uses **Bearer** tokens (refresh in OS keyring, access in memory); the browser admin/operator
  UI uses **httpOnly, Secure, SameSite=Strict cookies** + CSRF token. Server-authoritative timestamps + small JWT
  leeway guard against client clock skew.
- **Brute force:** `failed_logins` + `locked_until`; per-IP login rate-limit (in-process now; shared store if we go
  multi-worker, §11).
- **Bootstrap:** `python -m cloud_api create-admin`; all other users provisioned via the admin UI.

---

## 7. API design

Today's routers, hardened: every `/api/*` route gets an auth dependency, mutating routes get `require(...)` + an audit
write, and `GET /api/jobs` / `GET /api/campaigns` apply the ownership filter unless the caller holds `*.view_all`.

```
# Auth
POST /auth/login|refresh|logout      GET /auth/me

# Admin (users.manage / roles.manage / audit.view)
GET/POST /api/admin/users   PATCH /api/admin/users/{id}   PUT /api/admin/users/{id}/permissions
POST /api/admin/users/{id}/reset-password
GET/PUT /api/admin/roles[/{id}/permissions]   GET /api/admin/audit

# Global policy (settings.manage) — replaces file-based POST /api/config
GET/PUT /api/settings/crawl-policy

# Crawler-agent coordination (crawl.run; writes authorized by JOB OWNERSHIP, not the volatile grant — §17)
POST /api/jobs                 -> {job_id, seeds, policy, visited_bootstrap}
POST /api/jobs/{id}/leads      batch (server: enrich-dedup + score + attribution + occurrences)
POST /api/jobs/{id}/visited    batch
POST /api/jobs/{id}/heartbeat  {metrics} -> {cancel_requested}
POST /api/jobs/{id}/finish     {status}
POST /api/jobs/{id}/resume     interrupted -> running
POST /api/jobs/{id}/cancel     own; admin via crawl.cancel_all
```

Domain **import moves server-side** (`domains.import`), rebuilding the shared catalog in one place (no cross-machine
race), single-flighted via a Postgres advisory lock with import status persisted in a DB row (not in-memory).

**The local BFF** keeps the paths the frontend already calls and forwards them upstream with the stored token
(same-origin → no CORS, no token in the browser), plus local-only `GET /api/system/activity` /
`POST /api/system/cancel-all` reflecting *this machine's* tasks.

---

## 8. The local agent

Engine and parser internals do **not** change — only the data sink and a login/config layer.

- **`CloudApiClient`** mirrors the `Database` method surface the engine used, so call sites barely change:
  `save_lead`/`mark_visited`/`update_job_metrics`/`finish_job` become batched API writes; the recrawl-visited set
  arrives once as `visited_bootstrap` (scoped to the seed domains' netlocs to bound payload size, §17); the heartbeat
  response carries `cancel_requested`, which the worker checks where it checks local cancel today.
- The single-thread `db_pool` (a SQLite-serialization workaround) is gone; writes go through the durable outbox
  (§10.3), so cloud latency stays off the crawl hot loop and a blip never drops leads.
- **`local_store.py`** — a tiny SQLite (`local.db`): `local_config`, `auth_state`, `outbox`, `job_frontier`.
  Migrated by a `PRAGMA user_version` step-runner (§14), not Alembic. Refresh token in the OS keyring.
- **Launcher** — adds a login screen; start/stop, activity polling, and the confirm→cancel-all→drain shutdown are
  unchanged ([launcher/app.py](launcher/app.py)); the local server binds **loopback only**.

---

## 9. Concurrency & write-race strategy

Handling ~5-6 concurrent crawlers writing to one shared DB:

1. **Postgres removes the core constraint** — MVCC handles many writers; the cloud API uses `QueuePool`
   (`pool_size~10, max_overflow~10, pool_pre_ping=True`). The old single-writer SQLite limit is gone.
2. **All writes funnel through one API tier**, so the concurrency Postgres sees is well-shaped.
3. **Lead dedup is race-safe *and* non-lossy** — `INSERT ... ON CONFLICT (email) DO UPDATE` enriches (COALESCE nulls,
   keep higher confidence) + records a `lead_occurrences` row. Concurrent finders serialize on the row lock.
4. **Visited marking** — `ON CONFLICT (url, job_id) DO NOTHING`; idempotent across retries/crawlers.
5. **Job metrics don't contend** — written only by the owning crawler. A silent heartbeat past the reap threshold
   (§17) flips the job to `interrupted` (resumable, non-destructive — §10), never a phantom "running".
6. **Shared-lead edits** — last-write-wins with `updated_by`/`updated_at` + audit at this scale.
7. **SMTP dispatch centralized on the VPS (single worker)** — the existing in-process per-credential pacing then works
   unchanged; sending is always-on. The API tier no longer needs the in-memory task registry (job state lives in
   Postgres), so it can scale to multiple workers freely; the dispatcher stays a single instance.

---

## 10. Failure, fallback & resume

**Both.** No extracted lead is ever lost (durable outbox + idempotent cloud writes); an interrupted crawl **resumes**
from a checkpoint; emails are biased **at-most-once** so a crash never double-mails a recipient.

### 10.1 Principles
Durable local outbox (write-ahead) → idempotent cloud writes (safe at-least-once retry) → heartbeat liveness →
explicit resumable state (`running → interrupted → running`).

### 10.2 Failure matrix

| Scenario | Lost | Survives | Recovery |
|----------|------|----------|----------|
| Cloud transient blip | nothing | outbox on disk; crawl keeps extracting | flusher retries w/ backoff |
| Cloud extended outage | can't *start* a new job | running crawl buffers to outbox | drains on reconnect; job reconciled from `interrupted` |
| Local crash / power loss | in-memory frontier since last checkpoint | flushed data + last outbox fsync + last frontier ckpt | relaunch → flush → **resume** |
| Local offline at login | — | cached token (within TTL) | offline ⇒ no new work; running crawl keeps buffering |
| VPS restart mid-dispatch | possible *ambiguous* sends | QUEUED/SENT in PG | `SENDING` rows reconciled at-most-once (§10.5) |
| VPS disk loss | data since last backup | nightly `pg_dump` + offsite | restore (§12) |
| Duplicate delivery | nothing | — | `ON CONFLICT` no-op |

### 10.3 Outbox (local SQLite)
`outbox(id, job_id, kind, payload, created_at, attempts, last_error)`, `PRAGMA synchronous=FULL` (survives power
loss). An async flusher drains oldest-first, batches by kind, deletes on ack, backs off on failure; `metric` rows
collapse to the latest per job. **Poison rows** (persistent 4xx) move to a dead-letter after N attempts + alert, so one
bad record can't block the queue. `finish` is delivered only when no pending (non-dead) rows remain for the job. A
size cap applies backpressure (slows link discovery) during long outages.

### 10.4 Resume
`job_frontier` (pending queue items + pagination chain state) checkpointed ~5 s + on graceful stop. Resume: relaunch →
flush outbox → `POST /api/jobs/{id}/resume` → cloud returns fresh `visited_bootstrap` + policy → engine reloads the
frontier ∪ cloud visited and continues. **Exact** with the checkpoint on. (Cross-machine resume — persisting the
frontier to the cloud — is optional, §18.)

### 10.5 Dispatch recovery (at-most-once)
A transient `sending` status is claimed **before** the SMTP call. On restart, ambiguous `sending` rows are marked
`failed` ("send interrupted — not auto-resent") and surfaced for manual review, never blindly resent (double-mailing
officials wrecks sender reputation). Single-worker dispatch keeps pacing correct across restarts (rebuilt from
`cooldown_until` / `last_sent_at`).

### 10.6 Reaping & reconciliation
A cloud sweep marks jobs stale after ~120-180 s of heartbeat silence (lenient vs. `per_url_timeout`=100 s + jitter) as
`interrupted`. Reconciliation: late-arriving heartbeat/finish revives the job non-destructively and buffered leads
land via the idempotent path.

### 10.7 DR targets
Normal-ops RPO ≈ 0 (Postgres WAL + local outbox). Catastrophic VPS loss RPO = last backup; RTO = reprovision + restore
(target < 1 h, with a **rehearsed** restore). Hourly WAL archiving tightens RPO if wanted (§18).

---

## 11. Caching / Redis

**Not initially.** At this scale: JWT is stateless (revocation = one indexed read); login lockout lives in the `users`
row; SMTP pacing is solved by the single-worker dispatcher; hot reads (categories/domains) are small + indexed (a 60 s
in-process TTL cache if ever needed). **Add Redis only** for: multi-worker shared rate-limit/pacing, local-dispatch
distributed pacing, a real-time admin dashboard (pub/sub), or sub-TTL session denylist. It's a ~10-line Compose add
when a trigger appears.

---

## 12. Hosting & deployment (single VPS)

```yaml
# docker-compose.yml (sketch)
services:
  db:         { image: postgres:16, volumes: ["pgdata:/var/lib/postgresql/data"], ports: [], restart: unless-stopped }
  migrate:    { build: ./cloud, command: "alembic upgrade head", env_file: .env, depends_on: [db], restart: "no" }  # one-shot
  api:        { build: ./cloud, env_file: .env, depends_on: [migrate], expose: ["8000"], restart: unless-stopped }
              # gunicorn -k uvicorn.workers.UvicornWorker -w 2 cloud.asgi:app
  dispatcher: { build: ./cloud, command: "python -m cloud dispatch-worker", env_file: .env, depends_on: [migrate], restart: unless-stopped }
  proxy:      { image: caddy:2, ports: ["80:80","443:443"], volumes: ["./Caddyfile:/etc/caddy/Caddyfile","caddy_data:/data"], depends_on: [api], restart: unless-stopped }
volumes: { pgdata: {}, caddy_data: {} }
```

- **Postgres never published** (no host port) — only `migrate`/`api`/`dispatcher` reach it on the compose network.
- **Migrations run once** via the one-shot `migrate` service; `api`/`dispatcher` wait on it. No per-worker startup DDL
  (§14).
- **TLS** via Caddy (auto Let's Encrypt). **ufw**: only 22 (key-only) + 80/443.
- **Secrets** (`.env` chmod 600 or Docker secrets): `POSTGRES_PASSWORD`, `JWT_SECRET`, `CREDENTIAL_ENC_KEY`,
  `DATABASE_URL`.
- **Backups:** nightly `pg_dump` → local + offsite; restore tested before go-live.
- **Ops:** structured logs, `GET /healthz`, uptime check, `unless-stopped` restart policy.
- **Current local hosting stays** — the launcher + local FastAPI remain the operator's entry point (they run the
  browser-based crawler); they just no longer own a DB of record. PyInstaller packaging
  ([GovCrawler.spec](GovCrawler.spec)) is unchanged in shape, minus the bundled DB, plus login + local store.

---

## 13. Security & standards alignment

Mapped to recognized bars so "secure" is measurable, not vibes. None change the architecture — they're the review
checklists.

**Checklist**
- [ ] Postgres localhost-only, never public; TLS everywhere; HSTS on the admin UI.
- [ ] argon2id passwords; never logged; login rate-limited + lockout.
- [ ] JWT secret + `CREDENTIAL_ENC_KEY` in env/secret store, **with a rotation path** (envelope encryption); loss of
      the enc key is unrecoverable-by-design.
- [ ] SMTP passwords **encrypted at rest**; decrypted only in the dispatcher.
- [ ] RBAC enforced server-side on every route; ownership filters in queries; never trust the client.
- [ ] Refresh-token revocation (`user_sessions` + `token_version`); short access TTL; rotated-token-reuse detection.
- [ ] Local BFF **loopback-only**; validate `Origin`/`Host` + CSRF token on BFF mutations (blocks browser CSRF /
      DNS-rebinding against the localhost service). CORS locked to the admin origin.
- [ ] Pydantic validation; SQLAlchemy ORM (parameterized) — no string SQL.
- [ ] `audit_log` append-only at the DB-grant level (app role has no `UPDATE`/`DELETE`); shipped offsite.
- [ ] Least-privilege Postgres role; SSH key-only; ufw default-deny; auto security updates.

**Standards mapping**
- **OWASP ASVS L2 + Top 10 / API Top 10** as the security acceptance bar (broken-object-level-auth ⇒ the ownership
  filters). **OAuth2/JWT (RFC 7519)**, **NIST 800-63B** password guidance.
- **12-Factor** (env config, stateless processes, logs as streams — why we killed the runtime config write + boot DDL)
  and **CIS Benchmarks** for the Docker/Postgres/OS baseline; image scanning + pinned bases in CI.
- **Data protection (PII):** the system stores personal data of named officials → treat as PII under **GDPR-style
  principles + India DPDP Act 2023**: minimization, retention limits, encryption in transit + at rest, and an
  **erasure/opt-out workflow** (delete a lead + tombstone its email so re-crawls don't resurrect it — dovetails with
  the blacklist). `audit_log` + `lead_occurrences` already answer "where did this contact come from / who touched it"
  for data-subject requests.
- **Email/anti-spam:** CAN-SPAM-style (honor unsubscribe, truthful headers, sender identity) + **SPF/DKIM/DMARC** on
  the sending domain — a further argument for centralized dispatch from a known domain over laptop IPs.
- **SRE:** explicit RPO/RTO with a rehearsed restore; DB migrations gated in CI; the fault-injection acceptance test
  (§19). Formal SOC 2 / ISO 27001 is out of scope but layers on later without rework — the primitives (RBAC, audit,
  encryption, least privilege) are in place.

---

## 14. Migrations (forward strategy + one-time data migration)

### 14.1 Going forward — three concerns, not one Alembic

One Alembic worked because there was one DB in one process. Now:

| Concern | Backend | Tool | When |
|---------|---------|------|------|
| **Cloud schema** | Postgres | **Alembic** (`cloud/alembic/`, Postgres-only) | once per deploy (one-shot `migrate` service) |
| **Local schema** | SQLite | **`PRAGMA user_version` step-runner** in `local_store.py` (no Alembic) | agent startup |
| **Wire contract** | HTTP + `shared/` DTOs | **API versioning + additive DTO evolution** | continuously |

- **Cloud:** forward-only in prod; `autogenerate` is a reviewed draft (it misses enums/defaults/data moves); backfills
  are explicit steps; **expand/contract** (add-nullable → backfill → switch → drop) for zero-downtime; **no
  per-worker startup DDL or boot-time score recompute** (recompute is a background job on weight change). CI runs
  `alembic upgrade head` from empty + `alembic check` (model drift fails the build).
- **Local:** keeps Alembic out of the agent (leaner exe; wrong model for hundreds of offline copies). Migrations must
  **preserve the outbox** (undelivered rows); `job_frontier`/config are disposable; schema version tracks the app
  release, so each agent migrates its own file on first launch of a new build.
- **Wire:** an old agent can hit a freshly-migrated cloud, so DTOs evolve additively (new optional fields, never
  rename/remove in place), `shared/` is semver'd, the agent sends its version and the cloud can warn/refuse; contract
  tests in CI.

### 14.2 One-time data migration (multi-source merge)

GovCrawler is single-user today, so there may be **one `govcrawler.db` per operator's machine**, each with its own
autoincrement PKs that **collide on merge**. This is a merge with ID remapping, not a copy
(`scripts/migrate_sqlite_to_pg.py`):

1. Stand up Postgres; `alembic upgrade head` (normalized schema, §4); `create-admin` + one `users` row per operator.
2. Seed `categories`/`org_types` from the distinct code/title pairs in the sources; seed `app_settings`
   (`crawl_policy`/`extraction`/`lead_score`) from the canonical `config.yaml`.
3. **Per source SQLite** (tagged with its owning user), inserting **without old PKs** and capturing `old_id → new_id`
   maps, importing in dependency order (domains → crawl_jobs (+`crawl_job_domains` from the old `domain_ids` JSON) →
   crawl_snapshots → leads → outreach), **repointing every FK** through the maps. Stamp `owner_id` /
   `first_captured_by`; create a `lead_occurrences` row per (lead, job); fold `test_campaigns` rows into `campaigns`
   with `kind='test'`.
4. **Cross-source lead merge:** on the global `UNIQUE(email)`, keep the earliest `captured_at` as canonical, **enrich**
   nulls from the others, add a `lead_occurrences` row per source capture, and **repoint** every
   `campaign_emails.lead_id` off dropped duplicates (a dangling FK would break dispatch history).
5. **Encrypt** plaintext SMTP passwords into `password_encrypted`; dedupe by `(host, username)`.
6. **Verify:** per-source + merged counts, zero dangling FKs, spot-check snapshot-derived lead fields; keep sources
   read-only + archived until verified; smoke-test a crawl + a test campaign before cutover.

> Single-machine reality collapses 3–4 to a one-source copy, but the script still remaps PKs so re-runs/later merges
> stay safe.

---

## 15. Repository & module structure

One **monorepo**, split by *tier* + a dependency-light `shared/` package (the single source of truth for anything both
tiers must agree on). Not separate repos — that would force duplicating DTOs/enums/permissions and version-syncing by
hand. The admin is **not** a separate app: it's `cloud/api/admin/` + `frontend/templates/admin/`, RBAC-gated.

```
GovCrawler/
├── shared/            # framework-light; imported by BOTH tiers, imports neither
│   ├── enums.py           # status literals (mirrors the TEXT+CHECK values)
│   ├── permissions.py     # capability catalog + role defaults — ONE source of truth
│   ├── schemas/           # Pydantic wire DTOs (JobCreate, LeadBatch, Heartbeat, ...)
│   └── scoring.py         # compute_lead_score (pure)         ← was portal/services/lead_scoring.py
├── cloud/             # THE VPS APP (≈ today's portal/ minus the crawler, + auth + admin)
│   ├── api/ (deps.py, auth.py, admin/, domains|jobs|leads|campaigns|..., coordination.py, settings.py)
│   ├── db/ (tables/, mixins/, database.py)   ← from portal/db/* (near-verbatim)
│   ├── services/ (campaign_service, csv_import, importer)   ← portal/services + GovScraper
│   ├── security/ (hashing, jwt, crypto)      dispatcher.py    alembic/    asgi.py    cli.py
├── agent/             # THE LOCAL APP (per machine)
│   ├── crawler/ (engine.py, parser.py)       ← MOVED from portal/crawler (internals unchanged)
│   ├── cloud_client.py    local_store.py     bff/    launcher/    paths.py    run.py
├── frontend/          # SHARED UI (operator via BFF, admin via cloud) — templates/ + static/ + admin/ pages
├── deploy/            # docker-compose.yml, Caddyfile, .env.example, backup/restore
├── requirements/      # shared.txt · cloud.txt (psycopg/alembic/argon2, no playwright/tk) · agent.txt (playwright/tk, no psycopg)
├── GovCrawler.spec    # PyInstaller — bundles agent + shared + frontend ONLY
└── tests/             # tests/shared · tests/cloud · tests/agent
```

**Dependency direction (enforce with an `import-linter` CI contract):** `cloud → shared`, `agent → shared`, never
`cloud ↔ agent`. **Two build targets:** the agent is the PyInstaller `.exe` (+ Chromium, no DB driver); the cloud is a
Docker image. **Sequencing:** carve out `shared/` first (Phase 0), add `cloud/security` + `auth` + `admin/` in place
while still a monolith, then split `cloud`/`agent` + hoist `frontend/` when the topology lands (mostly `git mv` +
import fixups + the `CloudApiClient` adapter — not a rewrite).

---

## 16. End-to-end workflows (DB · location · permission)

Legend — **Where:** `L` local, `V` VPS. **DB:** `PG` cloud Postgres, `SQLite` local, `KR` keyring.

### 16.1 Login
`L` reads `cloud_api_base_url` (SQLite) → `POST /auth/login` → `V` verifies argon2 + `is_active`/`locked_until` (PG
`users`) → issues JWT + refresh (PG `user_sessions`) + `user.login` (PG `audit_log`) → `L` stores refresh in `KR`,
access in memory. No offline password check.

### 16.2 Crawl job
| # | Step | Where | DB | Perm |
|---|------|:-:|----|------|
| 1 | Pick seeds, Start (browser→BFF) | L | — | `crawl.run` |
| 2 | `POST /api/jobs` (owner=caller) | L→V | — | `crawl.run` |
| 3 | Create `crawl_jobs`(+`crawl_job_domains`/`job_custom_urls`), freeze `crawl_snapshots`, compute seed-scoped `visited_bootstrap`, load policy | V | PG (several r/w) + `app_settings` r | `crawl.run` |
| 4 | Return `{job_id, seeds, policy, visited_bootstrap}` + `job.start` | V→L | PG `audit_log` | — |
| 5 | Engine seeds queue, checkpoints frontier | L | SQLite `job_frontier` | — |
| 6 | Fetch → parse → extract | L | — | — |
| 7 | Leads/visited → durable outbox | L | SQLite `outbox` | — |
| 8 | Flusher → `POST .../leads` & `.../visited` (idem.) → enrich-dedup + score + `lead_occurrences` | L→V | SQLite (del on ack) + PG `leads`/`lead_occurrences`/`visited_urls` | job-owner |
| 9 | Heartbeat ~2 s → `{cancel_requested}` | L→V | PG `crawl_jobs` | job-owner |
| 10 | `POST .../finish` after outbox drains | L→V | PG `crawl_jobs`+`audit_log` | job-owner |

Cancel: `POST /api/jobs/{id}/cancel` sets `cancel_requested`; the engine stops on the next heartbeat. Admin cancels
anyone's job via `crawl.cancel_all`. **Write authorization for an existing job is by job ownership + non-terminal
state, decoupled from the volatile `crawl.run` grant** (so revoking a permission mid-crawl can't strand the outbox,
§17).

### 16.3 Domains import (central)
`domains.import` → `V` runs it (advisory-lock single-flight; status in a DB row) → PG `domains` (+ `categories`/
`org_types` upserts) → `audit_log`.

### 16.4 Leads (shared pool)
Browse `GET /api/leads` (`leads.view`, no owner filter; `job_id` filter restricted to own jobs unless `jobs.view_all`)
· Edit `PUT` (`leads.edit`, recompute score, `updated_by/at`) · Export (`leads.export`, `lead.export` audit) · CSV
import (`leads.import`). All `V`/`PG`.

### 16.5 Campaign → dispatch
Create (`campaigns.manage`; blacklist-filter, render drafts → PG `campaigns`(`kind='production'`)/`campaign_emails`) →
review/select → assign credential pool (PG `campaign_credentials`) → dispatch (`campaigns.dispatch`; DRAFT→QUEUED) →
**dispatcher worker (V)** claims `queued→sending`, paces per credential, sends, `→sent/failed`, blacklists hard
bounces. Users see own campaigns unless `campaigns.view_all`.

### 16.6 Admin & settings
User/role/permission management + revoke + reset (`users.manage`), audit view (`audit.view`), global policy edit
(`settings.manage`; a score-weight change triggers a **background** lead-score recompute). All `V`/`PG`.

> **Local SQLite is touched only** at login (session) and during a crawl (outbox + frontier + runtime config).
> Everything else is Postgres on the VPS.

---

## 17. Edge cases & scalability

Forward-looking items not already resolved in the schema/sections above (severity: 🔴 with-phase · 🟡 in-phase · 🟢 later).

**Correctness / integrity**
- 🔴 Permission revoked mid-crawl mustn't strand the outbox → existing-job writes authorize on **job ownership**, not
  the `crawl.run` grant; a hard 403 preserves the outbox + alerts (§16.2).
- 🔴 Outbox poison rows dead-lettered; `finish` gated on outbox-empty (§10.3).
- 🟡 Reap threshold ~120-180 s (vs `per_url_timeout` 100 s + jitter); reconciliation makes false reaps harmless (§10.6).
- 🟡 `app_settings` concurrent admin edits → optimistic `updated_at` check.
- 🟢 Multiple concurrent local jobs share `workers` → cap total local worker coroutines.

**Security** — loopback BFF + CSRF/DNS-rebinding, enc-key rotation, append-only audit (all in §13).

**Scalability**
- 🔴 `visited_bootstrap` scoped to the seed domains' netlocs (not the whole global set) to bound payload/memory.
- 🟡 Prune `visited_urls` older than `recrawl_days` (they no longer protect); per-IP login limiter moves to a shared
  store once multi-worker.
- 🟢 `audit_log` monthly partition + retention; keyset (not OFFSET) pagination on the shared leads list; PgBouncer once
  API workers grow; documented scale-out path (split DB host, N API workers, read replica) — the single VPS is a
  vertical ceiling and the dispatcher is a monitored SPOF; add SMTP credentials for send throughput.

**Outreach quality**
- 🟡 Shared leads → two users can email the same official from different campaigns → a global "recently-contacted"
  suppression window + a "already in campaign X" flag at staging (ties to the PII/anti-spam posture, §13).
- 🟢 Per-user / per-campaign daily send quotas on top of `daily_send_limit` for fairness on shared credentials.

---

## 18. Open decisions to confirm

The plan already builds on the recommended default for each; confirm or override.

| # | Decision | Default applied | Override cost |
|---|----------|-----------------|---------------|
| A | SMTP credential ownership | **Org-shared** (managed by `credentials.manage`) | per-user ⇒ add `owner_id` + pool scoping |
| B | Password reset flow | **Admin sets/resets a temp password** (no email infra) | email invite/reset ⇒ tokenized links + SMTP |
| C | Cross-machine resume | **Off** (frontier local) | on ⇒ persist `job_frontier` to the cloud (extra write volume) |
| D | Role taxonomy | **Admin / Operator / Viewer** + per-user overrides | add roles (e.g. Manager with `*.view_all`, no `users.manage`) |
| E | Backup frequency | **Nightly `pg_dump` + offsite** | hourly WAL archiving ⇒ tighter RPO |
| F | Lookup-table titles | **`categories`/`org_types` normalized** (§4.1) | keep denormalized titles ⇒ revert to per-row titles |

---

## 19. Phased roadmap

Independently shippable; resilience is built in across Phases 2–5 (not bolted on), and the normalized schema (§4) is
the Phase-1/2 target — there's no separate "fix schema" phase.

- **Phase 0 — Auth foundation (still single-DB, local).** Carve out `shared/` (enums, `permissions.py`, `scoring.py`,
  first DTOs) first — it's the reuse backbone. Add `users/roles/permissions/sessions/audit` + Alembic; argon2;
  `/auth/*`; `get_current_user` + `require()`; gate existing routes; `create-admin`; launcher login. *Outcome:*
  current app behind login + RBAC; lowest-risk way to de-risk the hardest new subsystem.
- **Phase 1 — Postgres on the VPS.** Compose stack (db + one-shot migrate + api + proxy + TLS); the **normalized
  schema** (§4) via Alembic; the multi-source migration script (§14). *Outcome:* cloud DB of record; API over HTTPS.
- **Phase 2 — Cloud API = source of truth (idempotent).** Ownership columns + filters; enrich-dedup + `lead_occurrences`;
  unified campaigns; admin endpoints; central import (single-flight); credential encryption; resumable job states +
  heartbeat/cancel columns. *Outcome:* full multi-user model live; admin UI usable; the cloud half of the fallback in
  place.
- **Phase 3 — Local engine → cloud, with write durability.** `CloudApiClient`; job-create/heartbeat/finish;
  remote-cancel; local store + keyring; **durable outbox** as the primary write path. *Outcome:* crawlers write to the
  cloud, **no lead lost** on outage/crash.
- **Phase 4 — Resilience & resume.** Frontier checkpoint + `resume`; stale-job reaping + reconciliation; outbox
  backpressure + dead-letter; **fault-injection acceptance pass** (kill API mid-crawl, kill crawler mid-page, pull
  network → zero loss + exact resume). *Outcome:* interrupted crawls resume; no phantom "running".
- **Phase 5 — Dispatch + hardening.** VPS dispatcher; at-most-once `sending` recovery; security checklist (§13);
  brute-force limiter; CORS/CSRF; **backups + rehearsed restore**; monitoring + `/healthz`. *Outcome:* production-ready.
- **Phase 6 — Polish.** Admin real-time dashboard (Redis only if pub/sub wanted); optional cross-machine resume; WAL
  archiving; CI/CD; docs; packaging refresh.

---

## 20. What stays exactly the same

- **Crawler engine internals** — priority queue, HTTPX-first/Playwright-fallback, per-domain politeness, pagination
  election, recrawl protection ([portal/crawler/engine.py](portal/crawler/engine.py)).
- **The 6-stage extraction pipeline** and the `Lead` shape ([portal/crawler/parser.py](portal/crawler/parser.py)).
- **Lead scoring** (`compute_lead_score`) — now runs in the cloud `save_lead`, from the same inputs the parser
  produces.
- **Campaign lifecycle** (`draft → queued → sent/failed`), blacklist auto-add on hard bounce, credential
  cooldown/round-robin.
- **The launcher's** start/stop/drain flow and the operator's page-by-page experience.
- **`crawl_snapshots` decoupling** — leads still read frozen domain metadata immune to catalog refreshes.

The overhaul changes *where data lives, who may touch it, and how it's deployed* — not *what the crawler finds or how
it scores it*.
This is textbook-correct snapshotting, *not* a 3NF defect. Keep it exactly.
- **The new `lead_occurrences` table** (§20.1) — normalizes the many-to-many "which jobs surfaced this shared lead,"
  which the current single-`job_id`-on-lead model can't express.
- **Junction tables already done right** — `campaign_credentials`, and the new `role_permissions`/`user_permissions`.