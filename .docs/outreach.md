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
- **Credential** (`smtp_credentials`) — an SMTP account, `provider` `basic` (password) or
  `microsoft`/`google` (OAuth2/XOAUTH2). The password/refresh/access tokens are all Fernet-encrypted
  at rest and decrypted only in the dispatcher. Port 465 → implicit TLS, 587 → STARTTLS. See
  [OAuth2 SMTP credentials](#oauth2-smtp-credentials-microsoftgoogle) below.

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
    - **rate limit** (421/450/451) → 1 h cooldown on the credential, `requeue_email` back to `QUEUED`, retry;
    - **auth failure** → `disable_credential`, `requeue_email` back to `QUEUED`, retry (not marked failed);
    - **connect/OS/timeout** → 15 min cooldown, `requeue_email` back to `QUEUED`, retry;
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

The `SENDING` claim is taken **before** the SMTP call. A retryable failure (auth, rate limit, network) calls
`requeue_email` to flip that email straight back to `QUEUED` before the loop retries. As a safety net for the
case that never reaches that handler — a process crash mid-send — `recover_stuck_sending(600)` requeues any
email left in `SENDING` past the threshold; it runs at startup and then periodically (every 60 s) in both
dispatch modes, not just once. Retried, never silently dropped, but never blindly re-sent mid-flight either.
Double-mailing officials wrecks sender reputation, so ambiguity resolves toward "retry from a clean claim,"
not "send twice." See [resilience.md](resilience.md#dispatch-recovery).

## Blacklist & credential health

Hard bounces auto-add the recipient (and its domain) to `blacklist`; new campaigns filter against it at
render time via `campaign_service.is_blacklisted()`, which checks both the **email** (case-insensitively)
and the **domain** — blocking `example.gov.in` suppresses every address at that domain, not just the one
that bounced. Credentials expose health (sent/failed totals, sent-today) and honor `daily_send_limit` — a
credential at its limit is excluded from the pool. `POST /api/credentials/{id}/test` does a live connect +
login and auto-activates on success / disables on failure.

## OAuth2 SMTP credentials (Microsoft/Google)

Exchange Online (and Gmail, years earlier) dropped SMTP AUTH with a plain password — sending through
those mailboxes needs OAuth2 (Authorization Code + PKCE) instead. `smtp_credentials.provider` is
`basic` (unchanged — username + Fernet-encrypted password), `microsoft`, or `google`; the latter two
carry Fernet-encrypted `refresh_token`/`access_token` + `token_expires_at` instead of a password.

**One consent per mailbox, no tenant-admin consent required** — the Microsoft app is registered
multi-tenant + personal accounts (`common` endpoint), so mailboxes across different organizations
(or personal outlook.com/hotmail addresses) each just sign in once, individually.

**Connect flow** (`frontend/agent/templates/settings.html`'s SMTP Credentials tab):

1. Create the credential with `provider: microsoft|google`, host/port pre-filled
   (`smtp.office365.com:587` / `smtp.gmail.com:587`), no password.
2. Click **Connect via Microsoft/Google** — `POST /api/credentials/{id}/oauth/start` returns an
   `authorize_url` (built by `cloud/security/oauth.py`'s authlib wrapper, PKCE state persisted in
   `oauth_pending_flows`); the frontend opens it in a new tab.
3. Sign in and consent on Microsoft's/Google's own page — this tab never touches the agent or cloud.
4. The provider redirects the browser directly to the cloud's public
   `GET /oauth/callback/{provider}` (bare, unauthenticated — mirrors `auth.router`'s callback-style
   mounting; the state param, not a session, ties it back to the credential). The cloud exchanges the
   code for tokens, stores them, flips the credential active, and shows a plain "Connected" page.
5. Back in Settings, click Refresh — the credential now shows **Connected**.

**Sending**: `cloud/api/dispatcher.py:_send_one_email` and `POST /api/credentials/{id}/test` both
branch on `provider`. Non-`basic` credentials call `cloud/security/oauth.get_valid_access_token()`
(refreshes if `token_expires_at` is within 60s, persisting the new pair via
`Database.update_credential_tokens`) then `aiosmtplib.SMTP.auth_xoauth2()` — aiosmtplib's native
XOAUTH2 support (added in 5.1.0) in place of `smtp.login()`. A revoked-consent/expired-refresh-token
failure (`OAuthTokenError`) is caught everywhere `SMTPAuthenticationError` already was, so it disables
the credential and retries exactly like a wrong password does.

**One-time setup** (per deployment, not per credential):

- **Azure AD** — register a public client app (no client secret). Supported account types:
  "Accounts in any organizational directory and personal Microsoft accounts". Redirect URI (platform
  "Web"): `{oauth.redirect_base_url}/oauth/callback/microsoft`.
  API permissions: **API permissions → Add a permission → APIs my organization uses** tab, search
  the exact string **"Office 365 Exchange Online"** (searching "Exchange" alone often returns
  nothing; if it still doesn't show, search by its GUID
  `00000002-0000-0ff1-ce00-000000000000` — if that also comes up empty, the tenant likely has no
  Exchange Online plan associated) → **Delegated permissions** (not Application permissions — that's
  the client-credentials/app-only flow this project doesn't use) → `SMTP.Send` → Add permissions,
  then **Grant admin consent**. `offline_access` is a standard OIDC scope, not something you add
  here — it's requested automatically by the authorize URL.
  **Do not** use the `SMTP.Send` permission listed under *Microsoft Graph* — same name, different
  resource/audience (`graph.microsoft.com` vs `outlook.office365.com`); a Graph-scoped token is
  rejected by the SMTP server. Put the resulting client id in `oauth.microsoft.client_id`
  (`OAUTH_MS_CLIENT_ID`).
- **Google Cloud** — enable the Gmail API, create an OAuth client of type **Web application** (not
  Desktop — this is a confidential server-side exchange with a fixed redirect URI and a real
  `client_secret` kept only in `config.yaml`/env). Redirect URI:
  `{oauth.redirect_base_url}/oauth/callback/google`. Scope: `https://mail.google.com/`. Put the
  resulting client id/secret in `oauth.google.client_id`/`client_secret`
  (`OAUTH_GOOGLE_CLIENT_ID`/`OAUTH_GOOGLE_CLIENT_SECRET`).

See [configuration.md](configuration.md#oauth) for where these values live.
