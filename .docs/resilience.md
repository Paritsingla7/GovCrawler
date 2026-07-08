# Resilience, Failure & Resume

The guarantee: **no extracted lead is ever lost**, an interrupted crawl **resumes** from a checkpoint, and
emails are biased **at-most-once** so a crash never double-mails a recipient. The chain is: durable local
outbox (write-ahead) → idempotent cloud writes (safe at-least-once retry) → heartbeat liveness → explicit
resumable state.

## Durable outbox — `agent/local_store.py`

Leads and visited URLs are written to a per-job SQLite outbox (`PRAGMA synchronous=FULL`, so a queued row
survives power loss, not just a clean crash), never straight to the cloud. `CloudApiClient`'s async flusher
drains oldest-first, batching by kind (up to 100 rows), and deletes rows only on a successful ack:

- **Retry with backoff** — a failed batch increments per-row `attempts` and sleeps before retry.
- **Dead-letter** — a row that fails `MAX_ATTEMPTS` (8) times moves to `outbox_dead` and is logged, so one
  poison record can't wedge the queue.
- **`finish` gated on drain** — `finish_job` flushes until the outbox is drained (or a 30 s deadline) before
  posting the terminal status; it warns if dead-lettered rows remain.
- **Backpressure** — when the outbox exceeds 5000 pending rows, the engine stops discovering *new* links
  (`is_backpressured`) so the buffer can drain during an extended cloud outage; already-queued work continues.

## Idempotent cloud writes

The coordination endpoints are safe to retry:

- **Leads** — global `UNIQUE(email)` with **enrich-on-conflict**: a re-delivered or duplicate lead fills
  null fields and keeps the higher confidence band rather than discarding data, and records a
  `lead_occurrences` row for per-job attribution.
- **Visited** — `UNIQUE(url, job_id)`, insert-or-ignore.

So an at-least-once flush (the outbox may resend after a blip) can never create duplicates or lose data.

## Frontier checkpoint & resume

`CrawlerEngine._checkpoint_loop` saves a frontier snapshot every 5 s (and on graceful stop) via
`CloudApiClient.save_frontier`. The snapshot captures the pending/in-flight queue items, the visited set,
counters, and — critically — the **shared pagination `chain_budget`** (reconstructed by `chain_key` on
rehydrate so a naive per-item restore can't reset the fan-out cap).

Resume (`POST /api/jobs/{id}/resume`): flush the outbox → load the frontier → rehydrate the queue and
visited set (unioned with a fresh `visited_bootstrap` from the cloud) → continue. With a checkpoint present
the resume is **exact**; without one (e.g. a fresh machine), the job re-crawls from seeds using only the
cloud's `visited_bootstrap` for dedup.

### Cross-machine resume (optional, off by default)

The frontier is always written locally. When `crawler.cross_machine_resume` (env `CROSS_MACHINE_RESUME`) is
on, `save_frontier` **also** best-effort uploads the snapshot to `POST /api/coordination/jobs/{id}/frontier`
(a synchronous call, safe because it runs on the `db_pool` thread, not the event loop). If a job is resumed
on a machine with no local checkpoint, `load_frontier` falls back to `GET .../frontier` from the cloud
(`job_frontiers` table) instead of re-crawling. The cost is extra write volume, which is why it's off by
default (`plan.md` §18 Decision C).

## Heartbeat, reaping & reconciliation

The engine heartbeats every 2 s with metrics; the response carries `cancel_requested` (that's how a
coordination cancel propagates into the local task). The cloud lifespan runs a **reaper** every 60 s
(`reap_stale_jobs`, 150 s threshold — lenient vs the 100 s `per_url_timeout`): a `RUNNING` job with a stale
or absent heartbeat is flipped to `interrupted` (resumable), never left as a phantom `running`. A
late-arriving heartbeat **revives** an `interrupted` job non-destructively, and any buffered leads land via
the idempotent path.

## Dispatch recovery (at-most-once)

The `SENDING` status is claimed atomically **before** the SMTP call. On startup (both API lifespan and the
standalone dispatcher) `recover_stuck_sending(600)` requeues any email left `SENDING` past 600 s — retried
from a clean claim, never blindly re-sent mid-flight. See [outreach.md](outreach.md#at-most-once-delivery).

## Failure matrix

| Scenario | Lost | Survives | Recovery |
|----------|------|----------|----------|
| Cloud transient blip | nothing | outbox on disk; crawl keeps extracting | flusher retries with backoff |
| Cloud extended outage | can't *start* a new job | running crawl buffers to outbox (backpressured) | drains on reconnect; job reconciled from `interrupted` |
| Local crash / power loss | in-memory frontier since last checkpoint | flushed data + last outbox fsync + last frontier ckpt | relaunch → flush → resume |
| VPS restart mid-dispatch | possible ambiguous send | QUEUED/SENT in Postgres | `SENDING` rows requeued at-most-once |
| VPS disk loss | data since last backup / WAL | nightly `pg_dump` + WAL archive | restore ([deployment.md](deployment.md#backups--recovery)) |
| Duplicate delivery to cloud | nothing | — | `ON CONFLICT` no-op / enrich |

## DR targets

Normal-ops RPO ≈ 0 (Postgres + local outbox). Catastrophic VPS loss: RPO = last backup (≤24 h with daily
`pg_dump`, or minutes with WAL archiving — `deploy/PITR.md`); RTO = reprovision + restore, target < 1 h with
a rehearsed restore. The manual acceptance runbook is `scripts/fault_injection_check.md` (kill API
mid-crawl, kill crawler mid-page, pull network → zero loss + exact resume).
