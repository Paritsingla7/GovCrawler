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
- Launcher — Bearer access token in memory, refresh token in the OS **keyring**; auto-refreshes on 401.
- Browser — `access`/`refresh` as **httpOnly, Secure, SameSite=Strict** cookies + a non-httpOnly `csrf`
  cookie for double-submit CSRF.

## Login / refresh / logout

- `POST /auth/login` — verify argon2id, check `is_active`/`locked_until`; on failure record it (audit
  `user.login_failed`); on success issue tokens, set cookies, audit `user.login`.
- `POST /auth/refresh` — **rotating**. Presenting a token whose session is already `revoked_at` triggers
  reuse detection: `revoke_session_family` + audit `user.session_reuse_detected` + 401. Otherwise rotate the
  session and issue a fresh access + refresh pair.
- `POST /auth/logout` — revoke the session by refresh-cookie hash, clear cookies.
- `GET /auth/bootstrap?token=` (loopback only) — the launcher hands the browser its session so the operator
  isn't asked to log in twice.

## Revocation

`get_current_user` re-checks two things on every request: `is_active` and `payload.tv ==
user.token_version`. **Bumping `token_version`** (on password change or explicit revoke) invalidates every
live access token for that user at once; worst-case exposure is one access-token TTL. Ordinary permission
edits take effect at the next refresh.

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
`owner_id == user.id` unless the caller holds the matching `*.view_all` permission (or is admin). `require_
loopback` restricts the launcher-only system endpoints to 127.0.0.1. Every mutating route writes an
`audit_log` row (append-only — the runtime DB role has no `UPDATE`/`DELETE` on it, Alembic 0020).

## Permission catalog (`shared/permissions.py`)

| Key | Guards |
|-----|--------|
| `users.manage` / `roles.manage` / `audit.view` | User & role admin; read the audit log |
| `settings.manage` | Edit global crawl policy / extraction / score weights |
| `domains.view` / `domains.import` | Browse catalog / trigger a central import |
| `crawl.run` / `crawl.cancel_all` | Create+start jobs (cancel own) / cancel any user's job |
| `jobs.view_all` | See all users' jobs (else own only) |
| `leads.view` / `leads.edit` / `leads.export` / `leads.import` | Shared-pool lead operations |
| `campaigns.manage` / `campaigns.dispatch` / `campaigns.view_all` | Create/edit own / dispatch / see all |
| `templates.manage` / `credentials.manage` / `blacklist.manage` | Outreach configuration |

## Built-in roles (`ROLE_DEFAULTS`)

| Capability group | Admin | Operator | Viewer |
|------------------|:-----:|:--------:|:------:|
| users / roles / audit / settings | ✔ | — | — |
| jobs.view_all / campaigns.view_all / crawl.cancel_all | ✔ | — | — |
| crawl.run / domains.import | ✔ | ✔ | — |
| leads.edit/export/import · campaigns.* · templates/credentials/blacklist.manage | ✔ | ✔ | — |
| domains.view / leads.view | ✔ | ✔ | ✔ |

`is_admin` short-circuits every check; a per-user `deny` does not apply to an admin (a limited "admin" is a
non-admin role with broad grants). Roles + the permission catalog are seeded idempotently by
`AuthMixin.seed_rbac()` on startup. Effective permissions = role bundle ± per-user `grant`/`deny` overrides.

## Bootstrap

Create the first admin with `python -m portal create-admin <email> [password]` (prompts for the password if
omitted). All other users are provisioned from the admin UI.
