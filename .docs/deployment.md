# Deployment (Docker / VPS)

The multi-user cloud tier runs as a Docker Compose stack on a single VPS. The desktop launcher
([architecture.md](architecture.md#subsystems)) is a separate, self-contained distribution path — this doc
covers the server.

## Quick start

```bash
cd deploy
cp .env.example .env          # fill in the secrets below
docker compose up --build -d
# first admin:
docker compose exec api python -m portal create-admin you@example.com
```

Caddy provisions TLS for `${DOMAIN}` automatically; `https://${DOMAIN}` is the only public port.

## Services (`deploy/docker-compose.yml`)

| Service | Image / command | Role |
|---------|-----------------|------|
| `db` | `postgres:16` (WAL archiving on) | Database of record; loopback-published `127.0.0.1:5432` only for the one-time migration script |
| `migrate` | `alembic upgrade head` (one-shot) | Applies migrations with the DDL-privileged `DATABASE_URL`; everything else waits on it |
| `api` | `python -m portal serve` | FastAPI + admin dashboard; `DISPATCH_MODE=external`; healthcheck `/healthz` |
| `dispatcher` | `python -m cloud.dispatch_service` | Standalone SMTP send loop (survives API restarts) |
| `proxy` | `caddy:2` | TLS termination + reverse proxy to `api:8001` |

All three app services build from one `deploy/Dockerfile` (a Playwright base image). Postgres is **never**
published beyond loopback; only the app services reach it over the Compose network.

## Secrets & environment (`deploy/.env`)

| Var | Purpose |
|-----|---------|
| `POSTGRES_PASSWORD` | Postgres superuser password |
| `DATABASE_URL` | Superuser URL — used by `migrate` for DDL |
| `DATABASE_URL_APP` + `GOVCRAWLER_APP_PASSWORD` | Least-privilege runtime role (`api`/`dispatcher`); no `UPDATE`/`DELETE` on `audit_log` (Alembic 0020) |
| `JWT_SECRET` (+ `JWT_SECRET_PREV`) | Token signing (+ rotation grace) |
| `CREDENTIAL_ENC_KEY` (+ `_PREV`) | SMTP-password Fernet key (+ rotation) |
| `DOMAIN` | Public hostname for Caddy TLS + `ADMIN_ORIGIN` |
| `CROSS_MACHINE_RESUME` | Opt-in cross-machine frontier fetch (off by default) |

Generate `JWT_SECRET` with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and
`CREDENTIAL_ENC_KEY` with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Migrations

`migrate` runs once with DDL rights, then `api`/`dispatcher` start against the least-privilege role. No
per-worker startup DDL. For a first cutover from an existing single-user SQLite DB, run
`scripts/migrate_sqlite_to_pg.py` (see its runbook and the caveat in the change report). See
[database-schema.md](database-schema.md#migration-chain).

## Hardening

Run `deploy/harden-vps.sh` once as root: ufw default-deny (allow 22/80/443), SSH key-only, unattended
security upgrades. The full checklist and rotation runbooks (JWT secret, credential key, least-privilege
role, CORS/CSRF) are in `deploy/SECURITY.md`.

## Backups & recovery

| RPO | Mechanism | Doc |
|-----|-----------|-----|
| ≤24 h | Daily `pg_dump` (`backup.sh` via cron) + `restore.sh` | `deploy/BACKUP.md` |
| minutes | WAL archiving (`archive_mode=on` in the `db` service) + point-in-time recovery | `deploy/PITR.md` |

Both `deploy/backups/` and `deploy/wal_archive/` must be copied **offsite** — they live on the same host.
Rehearse a restore before go-live (procedures in the two docs above).

## The admin dashboard

`/admin/dashboard` (requires `jobs.view_all`) polls `/api/admin/activity` every 3 s for active jobs +
per-campaign dispatch progress + a recently-finished tail. It uses polling rather than Redis pub/sub: the
dispatcher is a separate process, so in-process pub/sub couldn't see its progress anyway — true push would
require Redis regardless (`plan.md` §11 phrases Redis as "only if pub/sub wanted"). Redis push remains a
documented future upgrade.
