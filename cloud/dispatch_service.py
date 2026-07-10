"""
Standalone VPS dispatcher process (plan.md §19, Phase 5).

Desktop/dev installs keep dispatch embedded in the API process
(`dispatch.mode: embedded`, the default — see cloud/api/campaigns.py). The
Docker/VPS deployment instead sets `DISPATCH_MODE=external` on the `api`
service (so it never spawns dispatch itself) and runs this module as its own
`dispatcher` container/process — an API crash/restart no longer kills
in-flight campaign sends, and vice versa.

Usage:
    python -m cloud.dispatch_service

Polls for campaigns the API has flipped to RUNNING and have no locally
tracked task, spawns `run_campaign_dispatch` for each. Also periodically
recovers any email left stuck SENDING from a previous crash (checked
immediately at startup, then every _RECOVER_INTERVAL_SECONDS).
"""

import asyncio
import logging
import signal

from cloud.api.dispatcher import run_campaign_dispatch
from cloud.db import Campaign, CampaignStatus, Database
from portal.config import load_config

log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5
_STUCK_SENDING_THRESHOLD_SECONDS = 600  # same threshold as cloud/api/server.py's embedded path
_RECOVER_INTERVAL_SECONDS = 60  # same cadence as cloud/api/server.py's periodic recovery sweep


def _running_campaign_ids(db: Database) -> list[int]:
    with db._Session() as s:
        return [r[0] for r in s.query(Campaign.id).filter(Campaign.status == CampaignStatus.RUNNING).all()]


async def _poll_loop(db: Database, stopping: asyncio.Event) -> None:
    active: dict[int, asyncio.Task] = {}

    while not stopping.is_set():
        try:
            for campaign_id in _running_campaign_ids(db):
                task = active.get(campaign_id)
                if task is not None and not task.done():
                    continue
                log.info(f"Dispatcher picking up campaign {campaign_id}")
                active[campaign_id] = asyncio.create_task(run_campaign_dispatch(campaign_id, db))
        except Exception:
            log.error("Dispatcher poll iteration failed", exc_info=True)

        active = {cid: t for cid, t in active.items() if not t.done()}
        try:
            await asyncio.wait_for(stopping.wait(), timeout=_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

    if active:
        log.info(f"Shutting down — waiting for {len(active)} in-flight dispatch task(s) to finish their current send…")
        await asyncio.gather(*active.values(), return_exceptions=True)


async def _recover_loop(db: Database, stopping: asyncio.Event) -> None:
    """Periodic safety net alongside the dispatcher's own per-email requeue (issue #58):
    recover_stuck_sending() previously ran only once at process startup, so an email left
    SENDING by a mid-send crash sat stuck until this process was restarted."""
    while not stopping.is_set():
        try:
            recovered = db.recover_stuck_sending(_STUCK_SENDING_THRESHOLD_SECONDS)
            if recovered:
                log.warning(f"Requeued {len(recovered)} email(s) stuck SENDING from a previous crash: {recovered}")
        except Exception:
            log.error("Stuck-SENDING recovery sweep failed", exc_info=True)

        try:
            await asyncio.wait_for(stopping.wait(), timeout=_RECOVER_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    config = load_config()
    db = Database(config)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows dev runs — no POSIX signal handlers; Ctrl+C still raises KeyboardInterrupt

    log.info("Dispatch service started, polling every %ds", _POLL_INTERVAL_SECONDS)
    try:
        await asyncio.gather(_poll_loop(db, stopping), _recover_loop(db, stopping))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
