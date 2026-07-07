# WAL archiving + point-in-time recovery (Phase 6, plan.md §19)

Tightens the RPO beyond [BACKUP.md](BACKUP.md)'s daily `pg_dump` (≤24h): with WAL archiving
on, recovery can target any point in time up to the last archived segment — typically minutes,
bounded only by how far behind the archive is.

## What's running

`docker-compose.yml`'s `db` service starts Postgres with:
```
archive_mode = on
wal_level = replica
archive_command = test ! -f /wal_archive/%f && cp %p /wal_archive/%f
```
Completed WAL segments accumulate under `./wal_archive` (a plain bind-mounted directory next to
`docker-compose.yml`, same shape as `./backups`) — back this up offsite too, for the same reason
`BACKUP.md` gives for `backups/`: a dump/archive that only lives on the same VPS as the database
it protects doesn't survive a VPS-level failure.

`wal_archive/` grows without bound on its own — prune it after taking a new base backup (a base
backup + everything archived after it, up to now, is a full recovery chain; segments from before
the most recent base backup are no longer needed for PITR, though `pg_dump`-based `BACKUP.md`
restores don't depend on them at all).

## Point-in-time recovery procedure

Do this against a **scratch container**, not production, unless you're doing a real recovery:

1. Take a base backup (reuse `backup.sh`'s `pg_dump`, or `pg_basebackup` for a true binary base —
   `pg_dump` is sufficient here since WAL replay only needs to start from *some* consistent point
   at or before the target time, and this repo has no `pg_basebackup` automation yet).
2. Restore that base backup into a fresh Postgres 16 container:
   ```
   docker run --rm -d --name pitr-scratch -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:16
   gunzip -c backups/govcrawler_<timestamp>.sql.gz | docker exec -i pitr-scratch psql -U postgres
   ```
3. Copy the archived WAL segments generated *after* that backup's timestamp into a directory
   reachable by the scratch container (e.g. bind-mount `./wal_archive` read-only).
4. Set recovery config (Postgres 16: `postgresql.conf` + a `recovery.signal` file) —
   `restore_command = 'cp /wal_archive/%f %p'` and `recovery_target_time = '<target timestamp>'`,
   then start the container and let it replay WAL up to that point.
5. Sanity-check: `psql -U govcrawler -d govcrawler -c "SELECT count(*) FROM leads;"` and confirm
   the row count/timestamps match what you expect at the target time.

## RPO / RTO with WAL archiving

- **RPO**: minutes — bounded by archive lag (how promptly Postgres closes and archives each WAL
  segment), not the once-daily `pg_dump` cadence.
- **RTO**: base-backup restore time (same as `BACKUP.md`) plus WAL replay time, which scales with
  how much WAL has accumulated since the base backup — take base backups often enough that this
  stays small (e.g. weekly, alongside the existing daily `pg_dump`s).

Rehearse this at least once, same as `BACKUP.md`'s restore rehearsal — an archive_command that's
never been proven to actually restore is not a backup you can rely on.
