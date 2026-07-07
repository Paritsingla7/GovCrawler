"""
CloudApiClient — mirrors the `Database` method surface the crawler engine used
to call directly, so engine call sites barely change (plan.md §8). Talks HTTP
to the coordination endpoints (`cloud/api/coordination.py`) instead of the DB;
`save_lead`/`mark_visited` are fire-and-forget writes into a durable local
outbox (`agent/local_store.py`) instead of synchronous DB calls — a crash or
network blip never loses a lead. `send_heartbeat`/`finish_job` talk to the
API directly (not outboxed) since they need to be timely/ordered.

`token_provider` is a zero-arg callable returning a fresh bearer token per
call, not a static string — a crawl can run far longer than one access
token's TTL, and minting a JWT is cheap (no DB round trip), so there's no
need to cache/refresh one.
"""

import asyncio
import logging
import time

import httpx

from .local_store import LocalOutbox

log = logging.getLogger(__name__)

_BATCH_SIZE = 100
_FLUSH_IDLE_SLEEP = 2.0
_FLUSH_BUSY_SLEEP = 0.5
_BACKPRESSURE_THRESHOLD = 5000


async def create_remote_job(base_url: str, token_provider, transport=None, **body) -> dict:
    """No job_id exists yet, so this can't go through a per-job CloudApiClient
    instance — a short-lived plain HTTP call instead. `transport` lets a
    caller with no live server (e.g. the `python -m portal crawl` debug CLI)
    hit the coordination routes in-process via httpx.ASGITransport instead of
    requiring uvicorn to already be running."""
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15, transport=transport) as http:
        r = await http.post("/api/coordination/jobs", json=body,
                            headers={"Authorization": f"Bearer {token_provider()}"})
        r.raise_for_status()
        return r.json()


async def resume_remote_job(base_url: str, token_provider, job_id: int, transport=None) -> dict:
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15, transport=transport) as http:
        r = await http.post(f"/api/coordination/jobs/{job_id}/resume",
                            headers={"Authorization": f"Bearer {token_provider()}"})
        r.raise_for_status()
        return r.json()


class CloudApiClient:
    def __init__(self, base_url: str, token_provider, job_id: int, outbox_path, transport=None,
                cross_machine_resume: bool = False):
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._job_id = job_id
        self._outbox = LocalOutbox(outbox_path)
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=15, transport=transport)
        self._flush_task: asyncio.Task | None = None
        self._cross_machine_resume = cross_machine_resume

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def start(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_loop())

    # ── Direct calls (not outboxed) ──────────────────────────────────────────

    async def send_heartbeat(self, metrics: dict) -> bool:
        r = await self._http.post(f"/api/coordination/jobs/{self._job_id}/heartbeat",
                                  json=metrics, headers=self._headers())
        r.raise_for_status()
        return bool(r.json().get("cancel_requested"))

    async def finish_job(self, status: str, error: str | None = None, drain_timeout: float = 30.0) -> None:
        deadline = time.monotonic() + drain_timeout
        while not self._outbox.is_drained(self._job_id) and time.monotonic() < deadline:
            await self._flush_kind("lead")
            await self._flush_kind("visited")
            if not self._outbox.is_drained(self._job_id):
                await asyncio.sleep(_FLUSH_BUSY_SLEEP)
        if not self._outbox.is_drained(self._job_id):
            log.warning(f"job {self._job_id}: outbox did not fully drain before finish "
                       f"(dead-lettered rows may exist) — finishing anyway")
        await self._http.post(f"/api/coordination/jobs/{self._job_id}/finish",
                              json={"status": status, "error": error}, headers=self._headers())

    # ── Outboxed writes (fire-and-forget, matches the old sync call shape) ──

    def save_lead(self, **fields) -> None:
        self._outbox.enqueue(self._job_id, "lead", fields)

    def mark_visited(self, url: str, job_id: int) -> None:
        self._outbox.enqueue(job_id, "visited", {"url": url})

    # ── Frontier checkpoint (survives a crash so a resume isn't a restart) ───

    def save_frontier(self, snapshot: dict) -> None:
        """Always writes the local checkpoint (fast, same-machine resume).
        When crawler.cross_machine_resume is on, also best-effort uploads to
        the cloud — called from a db_pool executor thread (see engine.py's
        `_save_checkpoint`), so a blocking sync HTTP call here doesn't block
        the event loop. A failed upload is logged and otherwise ignored: the
        local checkpoint is still authoritative for same-machine resume."""
        self._outbox.save_frontier(self._job_id, snapshot)
        if not self._cross_machine_resume:
            return
        try:
            with httpx.Client(base_url=self._base_url, timeout=15) as http:
                r = http.post(f"/api/coordination/jobs/{self._job_id}/frontier",
                              json={"snapshot": snapshot}, headers=self._headers())
                r.raise_for_status()
        except Exception as e:
            log.warning(f"job {self._job_id}: cloud frontier upload failed: {e}")

    async def load_frontier(self) -> dict | None:
        local = self._outbox.load_frontier(self._job_id)
        if local is not None or not self._cross_machine_resume:
            return local
        try:
            r = await self._http.get(f"/api/coordination/jobs/{self._job_id}/frontier",
                                     headers=self._headers())
            r.raise_for_status()
            return r.json().get("snapshot")
        except Exception as e:
            log.warning(f"job {self._job_id}: cloud frontier fetch failed: {e}")
            return None

    def clear_frontier(self) -> None:
        self._outbox.clear_frontier(self._job_id)

    # ── Backpressure ─────────────────────────────────────────────────────────

    @property
    def is_backpressured(self) -> bool:
        """True once the LOCAL outbox backlog (across all jobs on this
        machine) exceeds a fixed threshold — a long cloud outage should slow
        new link discovery, not grow this file without bound."""
        return self._outbox.pending_count() > _BACKPRESSURE_THRESHOLD

    # ── Flusher ───────────────────────────────────────────────────────────────

    async def _flush_loop(self):
        try:
            while True:
                flushed_lead = await self._flush_kind("lead")
                flushed_visited = await self._flush_kind("visited")
                await asyncio.sleep(_FLUSH_BUSY_SLEEP if (flushed_lead or flushed_visited) else _FLUSH_IDLE_SLEEP)
        except asyncio.CancelledError:
            pass

    async def _flush_kind(self, kind: str) -> bool:
        batch = self._outbox.pending_batch(kind, limit=_BATCH_SIZE)
        if not batch:
            return False
        path = "leads" if kind == "lead" else "visited"
        body = {"items": [b["payload"] for b in batch]} if kind == "lead" else \
               {"urls": [b["payload"]["url"] for b in batch]}
        try:
            r = await self._http.post(f"/api/coordination/jobs/{self._job_id}/{path}",
                                      json=body, headers=self._headers())
            r.raise_for_status()
            self._outbox.ack([b["id"] for b in batch])
            return True
        except Exception as e:
            log.warning(f"outbox flush ({kind}) failed for job {self._job_id}: {e}")
            for b in batch:
                self._outbox.fail(b["id"], b["job_id"], kind, b["payload"], str(e))
            await asyncio.sleep(1.0)
            return False

    async def best_effort_drain(self, timeout: float = 5.0) -> None:
        """Called on cancellation — a bounded attempt to flush before giving up,
        so a cancelled run doesn't strand more data than a crash would."""
        deadline = time.monotonic() + timeout
        while not self._outbox.is_drained(self._job_id) and time.monotonic() < deadline:
            await self._flush_kind("lead")
            await self._flush_kind("visited")

    async def aclose(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._http.aclose()
        self._outbox.close()
