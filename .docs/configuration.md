# Configuration Reference

Configuration is split across two backends (plan.md §19.1 Phase 8 / §3.2), transparent to both the Settings
UI and the job-create API:

- **Machine-local runtime** — `portal/default_config.yaml` (shipped template) → `portal/config.yaml` (live,
  gitignored copy; `deploy/config.docker.yaml` in containers). `portal.main.load_config()` reads it and
  applies environment-variable overrides (below). Covers workers, fetch-strategy toggles, timeouts, and bind
  address — anything that only affects this one box's performance.
- **Crawl policy** — the cloud `app_settings` table (`key='crawl_policy'`), managed by
  `AppSettingsMixin`/`Database.get_crawl_policy()`. Covers extraction rules, lead-score weights, and the
  crawl-filter knobs (depth, rate limit, target suffixes, pagination, …) — values that must be identical
  across every crawler, since they affect lead quality/consistency. Seeded once from `config.yaml`'s
  existing values the first time a DB migrates through 0022 (`Database._seed_app_settings`), then lives in
  the DB from then on — editing `config.yaml` by hand no longer has any effect on these keys.

Both are editable at runtime from the **Settings** page (`GET`/`POST /api/config`, the latter gated by
`settings.manage`) — the endpoint routes each field to the right backend internally; the wire contract
(flat JSON keys) is identical regardless of which backend a field lives in, so the frontend doesn't need to
know or care.

## Environment overrides (`load_config`)

| Env var                           | Overrides                              | Notes                                                                                  |
|-----------------------------------|----------------------------------------|----------------------------------------------------------------------------------------|
| `DATABASE_URL_APP`                | `database.uri`                         | **Takes precedence.** The least-privilege `govcrawler_app` runtime role                |
| `DATABASE_URL`                    | `database.uri`                         | Used only if `DATABASE_URL_APP` is unset; the `migrate` service uses this (DDL rights) |
| `DISPATCH_MODE`                   | `dispatch.mode`                        | `embedded` (desktop/dev) or `external` (VPS dispatcher)                                |
| `ADMIN_ORIGIN`                    | `auth.admin_origin`                    | Enables CORS for a separate admin origin                                               |
| `JWT_SECRET` / `JWT_SECRET_PREV`  | `auth.jwt_secret` / `..._prev`         | Rotation grace period                                                                  |
| `CREDENTIAL_ENC_KEY` / `..._PREV` | `auth.credential_enc_key` / `..._prev` | Fernet key + rotation                                                                  |

Secrets follow an env-first-else-persist rule: if the env var is set it wins (and nothing is written to disk
— right for containers); otherwise a value already in `config.yaml` is kept, else generated and persisted.

## Sections

### `database`

- `uri` — `sqlite:///portal/data/govcrawler.db` (default) or `postgresql://user:pass@host/db`.

### `api`

- `host` (default `127.0.0.1`; `0.0.0.0` in Docker), `port` (default `8001`).

### `auth`

- `jwt_secret` (auto-generated if blank), `access_ttl_minutes` (15), `refresh_ttl_days` (14),
  `cookie_secure` (false; true in Docker), `lockout_threshold` (5), `lockout_minutes` (15). See
  [authentication.md](authentication.md).

### `dispatch`

- `mode` — `embedded` or `external`. See [outreach.md](outreach.md#dispatch-modes).

### `oauth`

Required before a Microsoft/Google SMTP credential can be connected (`.gov.in` outreach mailboxes
are almost always Outlook/M365, which dropped SMTP basic auth) — see
[outreach.md](outreach.md#oauth2-smtp-credentials-microsoftgoogle) for the connect flow and the
one-time Azure AD / Google Cloud app registration steps.

- `redirect_base_url` — the cloud's own public base URL (e.g. `https://your-cloud-domain.com`);
  Microsoft/Google redirect the browser here after consent (`GET /oauth/callback/{provider}`).
  Env override: `OAUTH_REDIRECT_BASE_URL` (docker-compose derives it from `DOMAIN`).
- `microsoft.client_id` — Env override: `OAUTH_MS_CLIENT_ID`. No client secret: registered as a
  public client (Authorization Code + PKCE needs none).
- `google.client_id` / `google.client_secret` — Env overrides: `OAUTH_GOOGLE_CLIENT_ID` /
  `OAUTH_GOOGLE_CLIENT_SECRET`. Unlike the Microsoft/JWT/credential-encryption secrets, these are
  plain static values from a one-time app registration — there's nothing to auto-generate, so (unlike
  `credential_enc_key`) a blank value is never persisted back to `config.yaml`.

### `scraper`

- `category_filter`, `org_type_filter` — restrict a live india.gov.in import.

### `crawler`

**Machine-local** (stays in `config.yaml`; may legitimately differ per box):

| Key                                     | Default              | Meaning                                      |
|-----------------------------------------|----------------------|----------------------------------------------|
| `workers`                               | 10                   | Concurrent async workers                     |
| `httpx_first` / `playwright_fallback`   | true / false         | Fetch strategy toggles                       |
| `httpx_timeout`                         | connect 10 / read 30 | httpx timeouts (s)                           |
| `playwright_timeout` / `js_settle_time` | 45 / 3.0             | Playwright nav timeout (s) / settle wait (s) |
| `per_url_timeout`                       | 100                  | Per-page stall killer (s)                    |

**Policy** (`app_settings.crawl_policy`; identical across every crawler):

| Key                  | Default                                                                                                  | Meaning                                                                   |
|----------------------|----------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------|
| `max_depth`          | 4                                                                                                        | Max crawl depth per seed (0 = seed only)                                  |
| `recrawl_days`       | 30                                                                                                       | Skip URLs visited within N days                                           |
| `request_delay`      | 1.5                                                                                                      | Per-domain politeness spacing (s)                                         |
| `max_links_per_page` | `{0:100, 1:50, 2:40, default:20}`                                                                        | Per-depth link cap                                                        |
| `max_custom_urls`    | 50                                                                                                       | Cap on a custom-URL job's seeds                                           |
| `target_suffixes`    | `.gov.in`, `.nic.in`                                                                                     | Domains the crawler will follow (empty = all; custom-URL jobs pass empty) |
| `priority_keywords`  | contact, officer, directory, …                                                                           | URL substrings that get queue priority 0                                  |
| `skip_extensions`    | pdf, doc, xls, media, …                                                                                  | Path suffixes never fetched                                               |
| `js_indicators`      | SPA markers                                                                                              | Substrings that trigger the Playwright fallback                           |
| `user_agent`         | Chrome UA                                                                                                | Request UA                                                                |
| `pagination`         | `enabled: false`, `max_pagination_pages: 50`, `max_chain_children: 100`, `text_signals`, `param_signals` | See [crawler.md](crawler.md#link-discovery--pagination)                   |

### `extraction` (policy — `app_settings.crawl_policy`, moved wholesale)

- `email`: `enabled`, `regex`, `valid_suffixes` (`.gov.in`, `.nic.in`, `.res.in`, `.ac.in`, `.com`),
  `obfuscation` (bracketed `[at]`/`[dot]`/`[hyphen]` → `@`/`.`/`-`), `context_chars` (200).
- `max_input_chars` (200000; 0 = uncapped) — proximity-scan bound.
- `role_local_parts` (webmaster, info, admin, contact, support, helpdesk, grievance) — flagged as `role`/`org`.
- `confidence`: `high_rungs` (`mailto_tel`, `microdata`) — the rungs scored `HIGH`; everything else is `LOW`.
- `person`: `enabled`, `title_prefixes` (Shri, Smt, Dr, …), `designation_keywords`, `proximity_chars` (300).

### `lead_score` (policy — `app_settings.crawl_policy`)

- `weights`: `email_high` 20, `email_low` 10, `person_name` 40, `designation` 30, `phone` 10. Editable via
  `POST /api/config`'s `weight_*` fields (API-only for now — no Settings-page UI yet).

## Lead scoring

`shared/scoring.compute_lead_score(fields, confidence_band, channel_tag, weights)` returns 0–100. Manual
(`channel_tag == "manual"`) leads short-circuit to **0**. Otherwise: email present adds `email_high` if the
band is `HIGH` else `email_low`; `person_name` adds 40; `designation` adds 30; `phone` adds 10. Base fields
cap at 90 with phone as the reserved top slice to 100. `Database.recompute_lead_scores()` re-scores **all**
existing leads whenever `POST /api/config` actually changes a weight — not on every startup (that
unconditional recompute was removed in Phase 8; the one case it also covered, backfilling a freshly-added
`lead_score` column on an old DB, is now a narrower one-time trigger inside `_ensure_columns`).

## Editing at runtime

The Settings page reads `GET /api/config` (flattened crawler+extraction+weights) and saves via
`POST /api/config` (`settings.manage`), which coerces types, parses newline/comma list fields, and writes
each field to its backend — machine-local keys to `config.yaml`, policy keys to `app_settings` via
`Database.set_app_setting()`. Read-only display fields (regex, obfuscation map, user agent,
pagination text/param signals) are shown but not editable there — change them via a direct
`db.set_app_setting("crawl_policy", ...)` call.

## Agent-side configuration (`portal/agent_config.yaml` + `agent/localdb.py`)

The agent reads its own config file, `portal/agent_config.yaml` (shipped template:
`portal/default_agent_config.yaml`) via `portal.config.load_agent_config()` — a **separate file from the
cloud's** `config.yaml`, not a shared one, so a single machine can run a dev cloud server and an agent
against it side by side without either overwriting the other's config. It carries exactly one thing: the
agent's own local BFF bind address (`api.host`/`api.port`, default `127.0.0.1:8001`). Nothing else — no
`database`, `auth`, `dispatch`, `scraper`, or crawl-policy keys, since the agent never uses any of them
(crawl policy arrives from the cloud on every job create/resume instead, see
[api-reference.md](api-reference.md#agent-coordination--cloudapicoordinationpy-prefix-apicoordination)).

Everything else operational moves to `agent/localdb.py`'s `local_settings` table, a plain `sqlite3`
key/value store at `portal/data/agent_local.db` (plan.md §19.1 Phase 9 Part 2, 2.1):

| Key                  | Set by                                                    | Meaning                                                                                                                                    |
|----------------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| `cloud_api_base_url` | Launcher's first-run "Cloud Server URL" prompt            | The VPS this agent talks to for everything — auth, proxied shared-data calls, coordination                                                 |
| `agent_id`           | Minted once on first run (`agent/localdb.get_agent_id()`) | A durable UUID (never a real hostname) stamped onto `crawl_jobs.agent_hostname` at job creation; the only agent allowed to resume that job |

`agent/localdb.py` also holds `visited_history` — this machine's own visited-URL recrawl-protection data,
consulted against the shared `crawler.recrawl_days` policy value but never uploaded anywhere. See
[database-schema.md](database-schema.md#local-stores-agent-per-machine).

## PostgreSQL / production

Set `database.uri` (or `DATABASE_URL`/`DATABASE_URL_APP`) to a `postgresql://` URL. The recommended path is
the Docker Compose stack — see [deployment.md](deployment.md), which wires the least-privilege role,
dispatcher split, TLS, backups, and WAL archiving.
