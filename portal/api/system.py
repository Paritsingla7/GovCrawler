"""
System-level activity aggregation for the desktop control panel (run.py).

Registers routes:
  GET  /api/system/activity     → live counts of crawl jobs / campaigns
                                   (production or test) currently running
  POST /api/system/cancel-all   → cancel everything currently active

Exists so the Tkinter launcher can ask "is it safe to stop the server?" over
plain HTTP instead of reaching into asyncio task dicts from another thread.
"""

import logging
from fastapi import APIRouter, Depends

from . import campaigns as campaigns_module
from .deps import get_active_tasks, get_db, require_loopback
from .jobs import cancel_job_if_running
from ..db import Campaign, CampaignStatus, Database

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


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


@router.post("/api/system/cancel-all", dependencies=[Depends(require_loopback)])
async def cancel_all(db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    activity = _get_activity(db, active_tasks)

    cancelled_jobs = sum(
        1 for job in activity["crawl_jobs"] if cancel_job_if_running(job["id"], db, active_tasks)
    )

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
