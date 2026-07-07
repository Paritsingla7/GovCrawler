# Backups + rehearsed restore (Phase 5, plan.md §13)

## RPO / RTO

- **RPO (Recovery Point Objective): ≤24 hours.** `backup.sh` is designed to run once daily
  via cron; the most you can lose is one day's writes.
- **RTO (Recovery Time Objective): the time to run `restore.sh` + `alembic upgrade head`
  against the restored dump** — in practice a few minutes for a database this size (a
  `pg_dump`/`psql` round-trip, not a byte-for-byte volume restore). `restore.sh` prints the
  exact commands it ran so this is auditable after the fact.

Tighten the RPO by running `backup.sh` more often (e.g. every 6h) — it's idempotent and
cheap; nothing about the design assumes once-daily. For a tighter RPO than any cron interval can
give (minutes instead of hours), see [PITR.md](PITR.md)'s WAL archiving + point-in-time recovery.

## Setup

```
cd deploy
crontab -e
# add:
0 3 * * * cd /path/to/GovCrawler/deploy && ./backup.sh >> backups/backup.log 2>&1
```

`backups/` is a plain directory next to `docker-compose.yml` (not a named Docker volume) —
back it up offsite too (rsync/S3/whatever), since a dump that only lives on the same VPS as
the database it backs up doesn't survive a VPS-level failure.

## Rehearsed restore

Do this **before** you need it for real, and periodically thereafter (plan.md §13 explicitly
calls for a *rehearsed* restore, not just a script that's never been run):

1. Pick a recent dump from `backups/`.
2. `./restore.sh backups/govcrawler_<timestamp>.sql.gz` — confirms before dropping anything,
   stops `api`/`dispatcher` during the restore so nothing writes concurrently.
3. Sanity-check: `docker compose exec db psql -U govcrawler -d govcrawler -c "SELECT count(*) FROM leads;"`
   — compare against what you expect from that dump's timestamp.
4. `docker compose -f docker-compose.yml run --rm migrate` — confirms the restored schema is
   at the current Alembic head (a dump taken before a schema change needs this).
5. Load the app (`/healthz`, then the frontend) and confirm it behaves normally.

For a true rehearsal without touching the live VPS, do steps 1-4 against a scratch Postgres
container instead (`docker run --rm -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:16`, point
`restore.sh`/`psql` at that instead) — same procedure, zero risk to production data.
