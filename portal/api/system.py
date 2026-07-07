"""
System-level activity aggregation for the desktop control panel (run.py).

Registers routes:
  GET  /api/system/activity     → live counts of crawl jobs / campaigns / test
                                   campaigns currently running
  POST /api/system/cancel-all   → cancel everything currently active

Exists so the Tkinter launcher can ask "is it safe to stop the server?" over
plain HTTP instead of reaching into asyncio task dicts from another thread.
"""

import logging
from fastapi import APIRouter, Depends

from . import campaigns as campaigns_module
from .deps import get_active_tasks, get_db, require_loopback
from .jobs import cancel_job_if_running
from ..db import CampaignStatus, Database, TestCampaign

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


def _running_test_campaigns(db: Database) -> list[dict]:
    # No task handle exists for test-campaign dispatch (it's fire-and-forget),
    # so DB status is the only signal here. It can go stale if the process
    # was killed mid-dispatch in a previous run — a pre-existing gap, not
    # something this endpoint can fully close.
    with db._Session() as s:
        rows = (
            s.query(TestCampaign)
            .filter_by(status=CampaignStatus.RUNNING)
            .all()
        )
        return [{"id": c.id, "name": c.name} for c in rows]


def _get_activity(db: Database, active_tasks: dict) -> dict:
    crawl_jobs = []
    for job_id, task in active_tasks.items():
        if task.done():
            continue
        job = db.get_job(job_id)
        label = f"Job #{job_id}"
        if job:
            label = f"Job #{job_id} ({job['crawled_domains']}/{job['total_domains']} domains, {job['leads_found']} leads)"
        crawl_jobs.append({"id": job_id, "label": label})

    campaigns = []
    for campaign_id, task in campaigns_module._active_campaign_tasks.items():
        if task.done():
            continue
        campaign = db.get_campaign(campaign_id)
        campaigns.append({"id": campaign_id, "name": campaign["name"] if campaign else f"Campaign #{campaign_id}"})

    test_campaigns = _running_test_campaigns(db)

    return {
        "crawl_jobs": crawl_jobs,
        "campaigns": campaigns,
        "test_campaigns": test_campaigns,
        "total_active": len(crawl_jobs) + len(campaigns) + len(test_campaigns),
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

    for test_campaign in activity["test_campaigns"]:
        db.update_test_campaign_status(test_campaign["id"], CampaignStatus.CANCELLED)

    log.info(
        f"cancel-all: {cancelled_jobs} crawl job(s), {len(activity['campaigns'])} campaign(s), "
        f"{len(activity['test_campaigns'])} test campaign(s) signalled to stop"
    )

    return {
        "crawl_jobs_cancelled": cancelled_jobs,
        "campaigns_cancelled": len(activity["campaigns"]),
        "test_campaigns_cancelled": len(activity["test_campaigns"]),
        "message": "Cancellation signalled. Campaign dispatch loops may take up to ~90s to actually stop.",
    }
