# Authentication & RBAC

Auth and role-based access control are cloud-tier (`cloud/api/auth.py`, `cloud/api/deps.py`,
`cloud/security/`, `cloud/db/mixins/auth_mixin.py`). The permission catalog and role defaults are the single
source of truth in `shared/permissions.py`.

## Passwords

argon2id via `argon2-cffi` (`cloud/security/hashing.py`): `hash_password`, `verify_password`,
`needs_rehash`. Passwords are never logged. Brute force is bounded by `failed_logins` + `locked_until` on
`users` (`lockout_threshold`, `lockout_minutes`).

## Tokens

`cloud/security/jwt.py`:

- **Access token** — short-lived (`auth.access_ttl_minutes`, default 15) HS256 JWT with claims `sub`
  (user id), `tv` (token_version), `type: "access"`, `iat`, `exp`.
- **Refresh token** — opaque `secrets.token_urlsafe(32)`, stored **sha256-hashed** in `user_sessions`
  (`auth.refresh_ttl_days`, default 14).

**Clients:**

- Launcher — logs in **directly against the cloud** (`cloud_api_base_url`, not its own local server); Bearer
  access token cached in `agent/identity.py`, refresh token in the OS **keyring**; auto-refreshes on 401.
  Effective permissions ride along in every login/refresh response (`UserOut.permissions`) and are cached
  too, so a mid-session role change is picked up on the next refresh with no extra round trip.
- Browser (admin, direct cloud access) — `access`/`refresh` as **httpOnly, Secure, SameSite=Strict** cookies
    + a non-httpOnly `csrf` cookie for double-submit CSRF.
- Browser (operator, via the agent) — never sees the real cloud session at all. The agent's own local BFF
  (`agent/bff/local_auth.py`) hands it a local `session`/`csrf` cookie pair instead (see
  [architecture.md](architecture.md) and plan.md §19.1 Phase 9 Part 2) and forwards every proxied request
  upstream with the launcher's cached bearer token, server-side.

## Login / refresh / logout

- `POST /auth/login` — verify argon2id, check `is_active`/`locked_until`; on failure record it (audit
  `user.login_failed`); on success issue tokens, set cookies, audit `user.login`. The agent's own
  `agent/bff/local_auth.py:login` relays to this unmodified for the case an operator lands on `/login`
  directly rather than through the launcher.
- `POST /auth/refresh` — **rotating**. Presenting a token whose session is already `revoked_at` triggers
  reuse detection: `revoke_session_family` + audit `user.session_reuse_detected` + 401. Otherwise rotate the
  session and issue a fresh access + refresh pair.
- `POST /auth/logout` — revoke the session (refresh token in the body, from `agent/identity.py:logout`, or
  the `refresh` cookie from a direct browser session), clear cookies.

There is no `/auth/bootstrap` anymore — it existed only to hand a same-process browser tab the launcher's
session via a cookie; now that the agent is a genuinely separate process from the cloud, the equivalent
hand-off is the agent's own loopback-only `GET /local-bootstrap` (`agent/bff/local_auth.py`), which never
touches the cloud at all.

## Revocation

`get_current_user` re-checks two things on every request: `is_active` and `payload.tv ==
user.token_version`. **Bumping `token_version`** (on password change or explicit revoke) invalidates every
live access token for that user at once; worst-case exposure is one access-token TTL. Ordinary permission
edits take effect at the next refresh.

> **⚠️ Known gap (issue #58):** this one-TTL guarantee holds only for *access* tokens. `set_password()`
> bumps `token_version` but does **not** call `revoke_session_family()`, and `POST /auth/refresh` validates
> the refresh token by session-hash/expiry only — it never checks `token_version`. So a leaked/stolen
> **refresh** token survives a password reset and can keep minting access tokens for up to
> `refresh_ttl_days` (default 14). Fix pending: revoke the session family on password change (or check
> `token_version` in the refresh route).

## Secret rotation

`decode_token_with_rotation` tries `auth.jwt_secret` then `auth.jwt_secret_prev`, so `JWT_SECRET` can be
rotated with a grace window (`JWT_SECRET_PREV`) without logging everyone out. SMTP-credential encryption
mirrors this with `CREDENTIAL_ENC_KEY`/`_PREV` via `MultiFernet` (`cloud/security/crypto.py`) — see
`scripts/rotate_credential_encryption_key.py` and `deploy/SECURITY.md`.

## CSRF & CORS

- **CSRF** — `verify_csrf` is a no-op for safe methods and for requests carrying an `Authorization` header
  (Bearer is not CSRF-able). Otherwise the `csrf` cookie must equal the `X-CSRF-Token` header, else 403.
- **CORS** — added only when `auth.admin_origin`/`ADMIN_ORIGIN` is set (Caddy serves a single origin, so
  this is defense-in-depth): allows that origin, credentials, and the `Authorization`/`Content-Type`/
  `X-CSRF-Token` headers.

## Enforcement

```python
def require(*perms):
    async def dep(user = Depends(get_current_user)):
        if not user.has_all(perms):        # is_admin short-circuits to True
            raise HTTPException(403)
        return user
    return dep
```

Ownership is enforced inside handlers, not by a blanket dependency: list/detail reads filter to
`owner_id == user.id` unless the caller holds the matching `*.view_all` permission (or is admin).
Loopback restriction is purely an agent-tier concern now (`agent/bff/security.py:require_loopback` — the
cloud tier never had a loopback-only endpoint left once `/auth/bootstrap` and the launcher-facing
`/api/system/*` routes moved to the agent, plan.md §19.1 Phase 9 Part 2). Every mutating route writes an
`audit_log` row (append-only — the runtime DB role has no `UPDATE`/`DELETE` on it, Alembic 0020) — as of
plan.md §19.1 Phase 9 Part 2, 2.0 this now covers every mutating router (domains, leads, campaigns,
templates, credentials — never the password value — blacklist, settings, crawl jobs), not just user
administration, and is readable via `GET /api/admin/audit` (`audit.view`, paginated + filterable by user/
action-prefix/date-range).

## Permission catalog (`shared/permissions.py`)

| Key                                                              | Guards                                                 |
|------------------------------------------------------------------|--------------------------------------------------------|
| `users.manage` / `roles.manage` / `audit.view`                   | User & role admin; read the audit log                  |
| `settings.manage`                                                | Edit global crawl policy / extraction / score weights  |
| `domains.view` / `domains.import`                                | Browse catalog / trigger a central import              |
| `crawl.run` / `crawl.cancel_all`                                 | Create+start jobs (cancel own) / cancel any user's job |
| `jobs.view_all`                                                  | See all users' jobs (else own only)                    |
| `leads.view` / `leads.edit` / `leads.export` / `leads.import`    | Shared-pool lead operations                            |
| `campaigns.manage` / `campaigns.dispatch` / `campaigns.view_all` | Create/edit own / dispatch / see all                   |
| `templates.manage` / `credentials.manage` / `blacklist.manage`   | Outreach configuration                                 |

## Built-in roles (`ROLE_DEFAULTS`)

| Capability group                                                                | Admin | Operator | Viewer |
|---------------------------------------------------------------------------------|:-----:|:--------:|:------:|
| users / roles / audit / settings                                                |   ✔   |    —     |   —    |
| jobs.view_all / campaigns.view_all / crawl.cancel_all                           |   ✔   |    —     |   —    |
| crawl.run / domains.import                                                      |   ✔   |    ✔     |   —    |
| leads.edit/export/import · campaigns.* · templates/credentials/blacklist.manage |   ✔   |    ✔     |   —    |
| domains.view / leads.view                                                       |   ✔   |    ✔     |   ✔    |

`is_admin` short-circuits every check; a per-user `deny` does not apply to an admin (a limited "admin" is a
non-admin role with broad grants). The field name stays `is_admin` in code/DB/JWT — only the admin
dashboard's UI labels it "Super Admin" (New User modal, Users table column) to disambiguate it from the
built-in "Admin" *role*, which is just a permission bundle a `deny` override can still narrow. Roles + the
permission catalog are seeded idempotently by
`AuthMixin.seed_rbac()` on startup. Effective permissions = role bundle ± per-user `grant`/`deny` overrides,
settable via `PUT /api/admin/users/{id}/permissions/{key}` (`{"effect": "grant"|"deny"|null}`, null clears
the override) and visible on the admin dashboard's Users & Permissions panel. There is no custom-role
builder — the three built-in roles and their bundles stay fixed in `shared/permissions.py`; per-user
overrides are the only way to deviate from a role (plan.md §19.1 Phase 9 Part 2, 2.0).

## Bootstrap

Create the first admin with `python -m portal create-admin <email> [password]` (prompts for the password if
omitted). All other users are provisioned from the admin UI.
