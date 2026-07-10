# Branch Plan — GovCrawler issue #58 (Fable 5 audit)

Source: [github.com/Jaguar000212/GovCrawler/issues/58](https://github.com/Jaguar000212/GovCrawler/issues/58)
— "GovCrawler v3 Codebase Audit." Findings spot-verified against the actual tree (line numbers in the
issue drift from current code; re-grep before acting on any of them). Grouped by code area so each
branch stays narrow — sweep in the same-area "minor" findings from Part 3 while you're already in that
file, rather than opening a new branch per one-liner.

**Naming convention:** `bugfix/*` = broken/wrong behavior today. `feat/*` = new capability, nothing to
regress. `refactor/*` = structural/perf cleanup, behavior unchanged. `chore/*` = dead code / catalog
hygiene. `docs/*` = documentation only.

---

## Already done

### `docs/audit-corrections-58` — DONE (this session)
`.docs/` corrected everywhere issue #58 caught it lying: cloud tier is not admin-only (it duplicates
leads/campaigns pages — `architecture.md`, `directory-structure.md`), token-in-response-body
(`architecture.md`, `api-reference.md`), password-reset session survival (`authentication.md`),
confidence-band "kept the higher one" claim (`database-schema.md`, `resilience.md`), blacklist
domain-suppression claim (`outreach.md`). Just needs a commit.

### `bugfix/parser-enrichment-fixes` (current branch) — DONE except one item
Lead domain attribution (wrong `snapshot_id` when a crawl wanders off-seed) + parser dead-code/enrichment
fixes. Full detail in `PLAN_attribution_and_parser.md`. One remaining item folded in from this audit:
**issue #58 Part 2 §3** (confidence band frozen at 2 tiers; `save_lead` never upgrades a band on
re-capture) — same files this branch already touches (`parser.py`, `lead_mixin.py`), so it stays here
rather than opening a new branch.

---

## Next branches, by area

### 1. `bugfix/auth-session-security-58`
**Mandatory, no matter what:** issue #58 Part 4 §6 — **custom role CRUD**. `roles.manage` is a seeded,
documented, Admin-granted permission with **zero** enforcing route (Part 2 §2) — the permission catalog
currently lies about what the system can do. This has to ship as real `POST/PATCH/DELETE` role routes +
UI (create/edit/clone/delete), not just a comment fix — that's the "add no matter what" item. Everything
else in this branch is security hardening around the same auth files:
- **P1-4** password reset doesn't revoke refresh-token sessions (`set_password` never calls
  `revoke_session_family`; `/auth/refresh` never checks `token_version`) — up to 14 days of exposure
  after a reset, contradicts the documented "one access-token TTL" guarantee.
- **P1-6b** agent's `/auth/login` relay returns both tokens in the response body despite its own
  docstring claiming otherwise.
- **P2-2** `roles.manage` dead permission → becomes real role CRUD (the mandatory item above).
- Minor sweep (Part 3, Auth & Ops): no login rate-limiting (only per-account lockout exists), timing-based
  user enumeration on login, audit log records proxy IP instead of `X-Forwarded-For` behind Caddy, no
  password policy (empty/1-char accepted), no "last admin" lockout guard, no session-listing/revocation UI
  for a user's own sessions.
- Files: `cloud/api/auth.py`, `cloud/db/mixins/auth_mixin.py`, `cloud/api/deps.py`, `cloud/api/admin.py`,
  `shared/permissions.py`, `agent/bff/local_auth.py`.

### 2. `bugfix/dispatch-reliability-58`
- **P1-1** retryable SMTP failures (rate-limit/network/auth) strand emails in `SENDING` forever —
  `continue`s without requeuing, and `recover_stuck_sending` only runs once at API startup, never
  periodically.
- **P1-5** blacklist half-enforced: case-sensitive email comparison (mitigated in practice since
  `save_lead` lowercases on write, but still wrong at the comparison site) + the `domain` column on
  `blacklist` is stored but never read — domain-wide suppression does nothing despite being documented.
- Files: `cloud/api/dispatcher.py`, `cloud/db/mixins/outreach_mixin.py`, `cloud/services/campaign_service.py`,
  `cloud/api/server.py`, `cloud/dispatch_service.py`.

### 3. `bugfix/crawler-resilience-58`
- **P1-2** heartbeat + checkpoint loops (`_reporter`, `_checkpoint_loop`) only catch `CancelledError` — one
  transient network blip kills the loop permanently, and 150s later the reaper wrongly flips a healthy
  crawl to `interrupted`.
- **P1-3** per-depth `max_links_per_page` int-keys vs. JSON string-keys — settings silently revert to
  hardcoded defaults after a save→reload round-trip through `app_settings`.
- Minor sweep (Part 3, Crawler): no fetch retry/backoff (only the outbox flusher retries, not page
  fetches), one poison record fails an entire 100-row outbox batch, unbounded crawl traps (no query-string
  stripping, no global page/runtime budget), pagination picks the *first* accepted link instead of
  `rel="next"` (can loop), whole-frontier JSON rewritten every 5s competes with lead writes on the single
  `db_pool` thread, phone fan-out copies one number onto every phone-less lead on container-match failure,
  `visited_urls`/`leads_found` metrics overcount (count attempts, not successes).
- Files: `agent/crawler/engine.py`, `agent/crawler/pagination.py`, `cloud/api/config.py`.

### 4. `bugfix/catalog-import-transaction-58`
- **P1-7** `importer.py` calls `clear_domains()` (delete-all + commit) before repopulating row-by-row — a
  mid-import failure (network blip, malformed JSON) leaves the catalog empty/partial with no rollback;
  `crawl_job_domains` FKs may dangle.
- Minor sweep (Part 3, Database): N+1 + per-row commits on bulk writes (`domain_mixin.py`, `lead_mixin.py`),
  lead export loads the entire result set into memory (unbounded for a large pool), unindexed `ilike`
  search on 4+ columns, jobs list has no offset pagination (capped at 100, can't page further), per-job
  unique constraint vs. global email dedup intent mismatch, non-atomic config writes (yaml + app_settings
  as two separate uncoordinated writes).
- Files: `cloud/services/importer.py`, `cloud/db/mixins/domain_mixin.py`, `cloud/db/mixins/lead_mixin.py`,
  `cloud/db/database.py`, `cloud/api/imports.py`.

### 5. `refactor/frontend-xss-and-dedup-58`
- **P1-6a** stored XSS in agent `campaigns.js` — dynamic content (campaign names, emails, subjects, error
  strings) goes straight into `innerHTML`/`onclick` with no `esc()`, even though `esc()` is loaded and
  already used correctly in the cloud twin of the same file.
- **P2-5** admin-dashboard error UX is inconsistent — some tabs (`loadSystemStatus`, `loadRoles`) render a
  proper "Failed to load" state, others (`loadAdminActivity`, `loadUsers`, `loadAuditLog`) `catch { return
  }` silently and hang on "Loading…" forever.
- Minor sweep (Part 3, Frontend): ~600 lines duplicated between `agent/leads.js`↔`cloud/leads.js` and
  `agent/campaigns.js`↔`cloud/campaigns.js` (decide: extract shared logic into `frontend/shared/`, or
  accept the duplication as a documented tradeoff — either way stop letting fixes land in only one copy,
  which is exactly how the XSS gap above happened); `agent/**` uses raw `fetch()` instead of `apiFetch()`
  in places, losing the 401→login redirect; stale "restart the server" error message that should say
  "cloud unreachable"; unescaped `innerHTML` injection in `settings.js` (credentials/templates/blacklist
  entries).
- Files: `frontend/agent/static/js/*`, `frontend/cloud/static/js/*`, `frontend/shared/static/js/`.

### 6. `feat/job-resume-ui-58` (small, standalone)
- **P2-1** `POST /api/jobs/{id}/resume` exists and works but has zero UI callers — an `interrupted` job
  renders as a clickable list item that does nothing on click. Also: the agent currently never produces
  `interrupted` locally (only the cloud reaper can), so confirm the precondition is actually reachable
  before wiring the button.
- Files: `frontend/agent/static/js/base.js`, `agent/api.py`.

### 7. `feat/pdf-document-extraction-58`
- **P2-6** all document types (`.pdf`, `.doc(x)`, `.xls(x)`, `.ppt`) sit in `skip_extensions` and are
  silently dropped — government "who's who"/contact directories are very often PDFs, so this is a real
  coverage gap, not just a nice-to-have. (= audit Part 4 #5.)
- Files: `agent/crawler/engine.py`, `agent/crawler/parser.py` (new extraction adaptor), `portal/config.yaml`.

---

## Backlog — `feat/*`, not yet scheduled (Part 4 brainstorm, minus #6 which is mandatory above)

One branch each, when prioritized — none of these are "fix broken behavior," all are net-new:

- `feat/bounce-reply-processing` — IMAP poll of the bounce/reply mailbox; auto-blacklist soft/async
  bounces, mark replies. Biggest outreach-ROI gap per the audit.
- `feat/list-unsubscribe` — `List-Unsubscribe` header + opt-out token/landing page + auto-blacklist on
  opt-out. Deliverability/compliance (CAN-SPAM/GDPR).
- `feat/drip-sequences` — multi-template campaigns with delay/trigger rules.
- `feat/campaign-scheduling` — `scheduled_at` + send-window + per-hour throttle.
- `feat/session-management-ui` — list/revoke a user's own active sessions (`revoke_session_family`
  already exists server-side).
- `feat/self-service-password` — `/auth/change-password` (logged-in) + `/auth/forgot-password` (emailed
  time-limited token). Currently only CLI/admin reset exists.
- `feat/recurring-crawls` — cron-style `schedule_pattern` on crawl jobs.
- `feat/campaign-analytics` — sent/bounced/replied funnel + per-credential health dashboard (most raw
  metrics already exist in the DB).
- `feat/redis-pubsub-admin-activity` — replace the admin dashboard's 3s poll with push; already flagged
  as a documented future upgrade in `.docs/deployment.md`.
- `feat/lead-dedup-review-ui` — batch-list likely-duplicate leads (same email/similar name/phone) for
  manual merge review.
- `feat/export-webhooks` — CSV/JSON/parquet export + webhook push to external CRMs (Salesforce, HubSpot,
  Pipedrive, …).

---

## Suggested sequencing

1. Finish + commit `bugfix/parser-enrichment-fixes` (WI-9 remaining) and `docs/audit-corrections-58`.
2. `bugfix/auth-session-security-58` — highest security severity, and carries the mandatory role-CRUD item.
3. `bugfix/dispatch-reliability-58`, `bugfix/crawler-resilience-58`, `bugfix/catalog-import-transaction-58`
   — can run in parallel, disjoint files.
4. `refactor/frontend-xss-and-dedup-58` — after the backend branches, since it touches the same JS the
   backend branches' UIs render against.
5. `feat/job-resume-ui-58`, then `feat/pdf-document-extraction-58`.
6. Backlog `feat/*` branches as prioritized.
