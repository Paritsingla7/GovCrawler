# Email Outreach System

Turns harvested leads into templated email campaigns and sends them with per-credential rate-limit
handling, blacklisting, and at-most-once delivery. Campaign management lives in `cloud/api/campaigns.py`;
the SMTP send loop is `cloud/api/dispatcher.py`, run either in-process or by the standalone
`cloud/dispatch_service.py`.

## Concepts

- **Template** (`email_templates`) — a Jinja2 `subject` + `raw_body`, validated on save. Variables are lead
  fields (`{{ name }}`, `{{ designation }}`, `{{ department }}`, …).
- **Campaign** (`campaigns`) — a template applied to a set of recipients. `kind` is `production` (real
  leads) or `test` (dummy recipients). Owned by its creator; visible to others only with `campaigns.view_all`.
- **Campaign email** (`campaign_emails`) — one rendered draft per recipient, with a lifecycle status and an
  `is_selected` flag.
- **Credential** (`smtp_credentials`) — an SMTP account. The password is Fernet-encrypted at rest and
  decrypted only in the dispatcher. Port 465 → implicit TLS, 587 → STARTTLS.

## Templates

Create/edit via `POST`/`PUT /api/templates` (`templates.manage`). Both `subject` and `raw_body` are parsed
through a Jinja2 `Environment` on save; a syntax error returns 400 with the message. Rendering uses each
lead's fields; missing values render as clean fallbacks and, for the key personalization fields
(name/designation), mark the draft with `missing_fields` and deselect it so you don't mail an obviously
broken message.

## Campaign creation

`POST /api/campaigns` (`campaigns.manage`):

- **Production** — supply `lead_ids` + `template_id`. Leads are loaded, blacklist-filtered, rendered to
  drafts (`campaign_service.render_draft_emails`), and stored as `campaign_emails` (`DRAFT`). Optionally
  assign a credential pool. The campaign starts **PAUSED**.
- **Test** — supply `dummy_details` (or `POST /api/campaigns/parse-csv` a CSV first). Renders against dummy
  recipients; `lead_id` is null on those rows.

## Draft review

Before dispatch you can: edit a draft's subject/body (`PUT .../emails/{eid}`), toggle one draft
(`PATCH .../emails/{eid}/selection`) or all (`PATCH .../emails/selection-all`), delete a draft, or add more
leads (`POST .../emails`). Deselecting a `QUEUED` email drops it back to `DRAFT`.

## Dispatch

`POST /api/campaigns/{id}/dispatch` (`campaigns.dispatch`) validates there are selectable drafts and at
least one usable credential, flips the campaign to **RUNNING**, and queues selected drafts (`DRAFT → QUEUED`).
`run_campaign_dispatch(campaign_id, db)` then loops:

1. Re-read campaign status each iteration — `PAUSED`/`CANCELLED` breaks the loop (so a kill switch takes
   effect within one send cycle).
2. `claim_next_queued_email` — **atomically** flip one `QUEUED → SENDING` (the at-most-once claim).
3. Resolve the credential pool, pick one round-robin, and `_wait_for_credential_slot` — a 30–90 s pace keyed
   by **credential id** and shared across all campaigns, so different credentials send back-to-back while the
   same one is rate-limited.
4. `_send_one_email` via `aiosmtplib`. Outcomes:
   - **success** → `SENT`;
   - **hard bounce** (550/553, recipients refused) → add to `blacklist` + `FAILED`;
   - **rate limit** (421/450/451) → 1 h cooldown on the credential, retry;
   - **auth failure** → `disable_credential`, retry (email not marked failed);
   - **connect/OS/timeout** → 15 min cooldown, retry;
   - **no usable credential** → campaign auto-**PAUSED** with a `pause_reason`.

Completion flips the campaign to **COMPLETED**, or back to **PAUSED** if deselected drafts remain.

## Dispatch modes

`dispatch.mode` (config, overridable by the `DISPATCH_MODE` env var):

- **`embedded`** (default; desktop/dev) — the API process spawns `run_campaign_dispatch` as a task and the
  API lifespan owns stuck-`SENDING` recovery + the reaper.
- **`external`** (VPS) — `POST .../dispatch` only flips the campaign to RUNNING; the standalone
  `cloud/dispatch_service.py` process polls every 5 s for RUNNING campaigns and runs the loop. This keeps
  in-flight sends alive across API restarts.

Both modes share the same send loop and the same 600 s stuck-`SENDING` recovery threshold.

## At-most-once delivery

The `SENDING` claim is taken **before** the SMTP call. On restart, `recover_stuck_sending(600)` requeues any
email left in `SENDING` past the threshold — it is retried, not silently dropped, but never blindly re-sent
mid-flight. Double-mailing officials wrecks sender reputation, so ambiguity resolves toward "retry from a
clean claim," not "send twice." See [resilience.md](resilience.md#dispatch-recovery).

## Blacklist & credential health

Hard bounces auto-add the recipient (and its domain) to `blacklist`; new campaigns filter against it at
render time. Credentials expose health (sent/failed totals, sent-today) and honor `daily_send_limit` — a
credential at its limit is excluded from the pool. `POST /api/credentials/{id}/test` does a live connect +
login and auto-activates on success / disables on failure.
