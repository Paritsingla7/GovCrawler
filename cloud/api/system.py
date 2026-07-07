"""
System-level activity aggregation for the desktop control panel (run.py) and
the browser-facing admin dashboard.

Registers routes:
  GET  /api/system/activity     → live counts of crawl jobs / campaigns
                                   (production or test) currently running,
                                   loopback-only (desktop launcher)
  GET  /api/admin/activity      → same shape plus dispatch progress + a
                                   recently-finished tail, for the browser
                                   admin dashboard (permission-gated, not
                                   loopback-restricted)
  POST /api/system/cancel-all   → cancel everything currently active
  GET  /healthz                 → liveness/readiness probe (public, no auth)

Exists so the Tkinter launcher can ask "is it safe to stop the server?" over
plain HTTP instead of reaching into asyncio task dicts from another thread.
"""

import logging
from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from . import campaigns as campaigns_module
from .deps import get_active_tasks, get_db, require, require_loopback
from ..db import Campaign, CampaignStatus, Database
from agent.api import cancel_job_if_running

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(response: Response, db: Database = Depends(get_db)):
    """Public liveness/readiness probe for the proxy/orchestrator — no auth,
    no loopback restriction (deploy/docker-compose.yml's api healthcheck and
    any external uptime monitor both need to reach this)."""
    try:
        with db._Session() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        log.error("Health check failed — DB unreachable", exc_info=True)
        response.status_code = 503
        return {"status": "db_unreachable"}


def _running_campaigns_without_task(db: Database, known_ids: set[int]) -> list[dict]:
    # Test-campaign dispatch (and any campaign whose task handle was lost to a
    # process restart) has no task in _active_campaign_tasks, so DB status is
    # the only signal for those. Can go stale if the process was killed
    # mid-dispatch in a previous run — a pre-existing gap, not something this
    # endpoint can fully close.
    with db._Session() as s:
        q = s.query(Campaign).filter(Campaign.status == CampaignStatus.RUNNING)
        if known_ids:
            q = q.filter(Campaign.id.notin_(known_ids))
        return [{"id": c.id, "name": c.name} for c in q.all()]


def _get_activity(db: Database, active_tasks: dict) -> dict:
    crawl_jobs = []
    for job_id, task in active_tasks.items():
        if task.done():
            continue
        job = db.get_job(job_id, view_all=True)
        label = f"Job #{job_id}"
        if job:
            label = f"Job #{job_id} ({job['crawled_domains']}/{job['total_domains']} domains, {job['leads_found']} leads)"
        crawl_jobs.append({"id": job_id, "label": label})

    campaigns = []
    tracked_ids = set()
    for campaign_id, task in campaigns_module._active_campaign_tasks.items():
        if task.done():
            continue
        tracked_ids.add(campaign_id)
        campaign = db.get_campaign(campaign_id, view_all=True)
        campaigns.append({"id": campaign_id, "name": campaign["name"] if campaign else f"Campaign #{campaign_id}"})

    campaigns.extend(_running_campaigns_without_task(db, tracked_ids))

    return {
        "crawl_jobs": crawl_jobs,
        "campaigns": campaigns,
        "total_active": len(crawl_jobs) + len(campaigns),
    }


@router.get("/api/system/activity", dependencies=[Depends(require_loopback)])
async def get_activity(db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    return _get_activity(db, active_tasks)


def _get_admin_activity(db: Database, active_tasks: dict) -> dict:
    """Same live counts as `_get_activity`, plus per-campaign dispatch
    progress (from the existing `get_campaign_stats` aggregate) and a
    recently-finished tail — the browser admin dashboard polls this instead
    of `/api/system/activity` since it isn't running on localhost."""
    activity = _get_activity(db, active_tasks)
    for campaign in activity["campaigns"]:
        campaign["stats"] = db.get_campaign_stats(campaign["id"])

    recent_jobs = db.list_jobs(limit=5, view_all=True)
    recent_campaigns, _ = db.list_campaigns(limit=5, view_all=True)
    activity["recent_jobs"] = [j for j in recent_jobs if j["status"] not in ("pending", "running")]
    activity["recent_campaigns"] = [c for c in recent_campaigns if c["status"] not in ("RUNNING",)]
    return activity


@router.get("/api/admin/activity", dependencies=[Depends(require("jobs.view_all"))])
async def get_admin_activity(db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    return _get_admin_activity(db, active_tasks)


@router.post("/api/system/cancel-all", dependencies=[Depends(require_loopback)])
async def cancel_all(db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    """Loopback-only emergency stop for local shutdown — unlike the
    coordination cancel endpoint, this flips status to 'cancelled' directly
    rather than waiting for the engine to drain and self-report, since the
    whole point is "the server is about to go away right now"."""
    activity = _get_activity(db, active_tasks)

    cancelled_jobs = 0
    for job in activity["crawl_jobs"]:
        db.set_cancel_requested(job["id"])
        if cancel_job_if_running(job["id"], active_tasks):
            db.finish_job(job["id"], status="cancelled")
            cancelled_jobs += 1

    for campaign in activity["campaigns"]:
        db.update_campaign_status(campaign["id"], CampaignStatus.CANCELLED)

    log.info(
        f"cancel-all: {cancelled_jobs} crawl job(s), {len(activity['campaigns'])} campaign(s) signalled to stop"
    )

    return {
        "crawl_jobs_cancelled": cancelled_jobs,
        "campaigns_cancelled": len(activity["campaigns"]),
        "message": "Cancellation signalled. Campaign dispatch loops may take up to ~90s to actually stop.",
    }
