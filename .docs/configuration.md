# Configuration Reference

Configuration is a single YAML file. `portal/default_config.yaml` is the shipped template;
`portal/config.yaml` is the live, gitignored copy (created from the template on first run). In Docker,
`deploy/config.docker.yaml` is baked in as `portal/config.yaml`. `portal.main.load_config()` reads it and
applies environment-variable overrides (below). Crawl/extraction settings are also editable at runtime from
the **Settings** page (`GET`/`POST /api/config`, the latter gated by `settings.manage`), which writes back
to `config.yaml`.

> Note: `plan.md` proposed moving crawl policy into a cloud `app_settings` table. That table is **not**
> implemented — policy remains file-based in `config.yaml` and is delivered to a crawl as the job's `policy`
> payload at start. This is a deliberate deviation flagged in the change report.

## Environment overrides (`load_config`)

| Env var | Overrides | Notes |
|---------|-----------|-------|
| `DATABASE_URL_APP` | `database.uri` | **Takes precedence.** The least-privilege `govcrawler_app` runtime role |
| `DATABASE_URL` | `database.uri` | Used only if `DATABASE_URL_APP` is unset; the `migrate` service uses this (DDL rights) |
| `DISPATCH_MODE` | `dispatch.mode` | `embedded` (desktop/dev) or `external` (VPS dispatcher) |
| `ADMIN_ORIGIN` | `auth.admin_origin` | Enables CORS for a separate admin origin |
| `CROSS_MACHINE_RESUME` | `crawler.cross_machine_resume` | `1`/`true`/`yes` → true |
| `JWT_SECRET` / `JWT_SECRET_PREV` | `auth.jwt_secret` / `..._prev` | Rotation grace period |
| `CREDENTIAL_ENC_KEY` / `..._PREV` | `auth.credential_enc_key` / `..._prev` | Fernet key + rotation |

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

### `scraper`
- `category_filter`, `org_type_filter` — restrict a live india.gov.in import.

### `crawler` (policy — must be identical across crawlers)
| Key | Default | Meaning |
|-----|---------|---------|
| `workers` | 10 | Concurrent async workers |
| `max_depth` | 4 | Max crawl depth per seed (0 = seed only) |
| `recrawl_days` | 30 | Skip URLs visited within N days |
| `httpx_first` / `playwright_fallback` | true / false | Fetch strategy toggles |
| `cross_machine_resume` | false | Persist the frontier to the cloud for resume on another machine |
| `httpx_timeout` | connect 10 / read 30 | httpx timeouts (s) |
| `playwright_timeout` / `js_settle_time` | 45 / 3.0 | Playwright nav timeout (s) / settle wait (s) |
| `per_url_timeout` | 100 | Per-page stall killer (s) |
| `request_delay` | 1.5 | Per-domain politeness spacing (s) |
| `max_links_per_page` | `{0:100, 1:50, 2:40, default:20}` | Per-depth link cap |
| `max_custom_urls` | 50 | Cap on a custom-URL job's seeds |
| `target_suffixes` | `.gov.in`, `.nic.in` | Domains the crawler will follow (empty = all; custom-URL jobs pass empty) |
| `priority_keywords` | contact, officer, directory, … | URL substrings that get queue priority 0 |
| `skip_extensions` | pdf, doc, xls, media, … | Path suffixes never fetched |
| `js_indicators` | SPA markers | Substrings that trigger the Playwright fallback |
| `user_agent` | Chrome UA | Request UA |
| `pagination` | `enabled: false`, `max_pagination_pages: 50`, `max_chain_children: 100`, `text_signals`, `param_signals` | See [crawler.md](crawler.md#link-discovery--pagination) |

### `extraction`
- `email`: `enabled`, `regex`, `valid_suffixes` (`.gov.in`, `.nic.in`, `.res.in`, `.ac.in`, `.com`),
  `obfuscation` (bracketed `[at]`/`[dot]`/`[hyphen]` → `@`/`.`/`-`), `context_chars` (200).
- `max_input_chars` (200000; 0 = uncapped) — proximity-scan bound.
- `role_local_parts` (webmaster, info, admin, contact, support, helpdesk, grievance) — flagged as `role`/`org`.
- `confidence`: `high_rungs` (`mailto_tel`, `microdata`), `mid_rungs` (`table_block`, `proximity_text`).
- `person`: `enabled`, `title_prefixes` (Shri, Smt, Dr, …), `designation_keywords`, `proximity_chars` (300).

### `lead_score`
- `weights`: `email_high` 20, `email_low` 10, `person_name` 40, `designation` 30, `phone` 10.

## Lead scoring

`shared/scoring.compute_lead_score(fields, confidence_band, channel_tag, weights)` returns 0–100. Manual
(`channel_tag == "manual"`) leads short-circuit to **0**. Otherwise: email present adds `email_high` if the
band is `HIGH` else `email_low`; `person_name` adds 40; `designation` adds 30; `phone` adds 10. Base fields
cap at 90 with phone as the reserved top slice to 100. Because `Database._recompute_lead_scores()` runs on
every startup, changing the weights re-scores **all** existing leads, not just new ones.

## Editing at runtime

The Settings page reads `GET /api/config` (flattened crawler+extraction) and saves via `POST /api/config`
(`settings.manage`), which coerces types, parses newline/comma list fields, updates the in-memory config,
and persists `config.yaml`. Read-only display fields (regex, obfuscation map, user agent) are shown but not
editable there.

## PostgreSQL / production

Set `database.uri` (or `DATABASE_URL`/`DATABASE_URL_APP`) to a `postgresql://` URL. The recommended path is
the Docker Compose stack — see [deployment.md](deployment.md), which wires the least-privilege role,
dispatcher split, TLS, backups, and WAL archiving.
