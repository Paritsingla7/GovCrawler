"""Crawl-job read endpoints (ownership-filtered). Creation/cancellation are
agent-tier and live in agent/api.py. See .docs/api-reference.md."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from urllib.parse import urlsplit

from .deps import CurrentUser, get_current_user, get_db
from ..db import Database

log = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


def _normalize_custom_urls(urls: list[str], max_urls: int) -> list[str]:
    """Dedupes, defaults to http:// when no scheme is given, and validates —
    used by cloud/api/coordination.py's job-creation route for custom_urls
    jobs (imported locally there to avoid a jobs<->coordination cycle)."""
    normalized = []
    seen = set()
    invalid = []
    for raw in urls:
        candidate = raw.strip()
        if not candidate:
            continue
        if "://" not in candidate:
            candidate = "http://" + candidate
        netloc = urlsplit(candidate).netloc
        if not netloc:
            invalid.append(raw)
            continue
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    if invalid:
        raise HTTPException(status_code=422,
                            detail=f"Invalid URL(s): {', '.join(invalid)}")
    if not normalized:
        raise HTTPException(status_code=422, detail="No valid custom URLs provided")
    if len(normalized) > max_urls:
        raise HTTPException(status_code=422,
                            detail=f"Too many custom URLs ({len(normalized)}); max is {max_urls}")
    return normalized


@router.get("/api/jobs")
async def list_jobs(limit: int = Query(20, ge=1, le=100), db: Database = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    return db.list_jobs(limit=limit, owner_id=user.id, view_all=user.can("jobs.view_all"))


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: int, db: Database = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    """Status is read straight from the DB — no more `active_tasks`-based
    override. Heartbeat-driven status (updated by job_mixin.heartbeat/
    reap_stale_jobs) is now the single source of truth for both this and
    list_jobs, so the two can no longer disagree the way a live-task-only
    override could (e.g. a hung task that stopped heartbeating but whose
    asyncio.Task object was still alive)."""
    job = db.get_job(job_id, owner_id=user.id, view_all=user.can("jobs.view_all"))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/api/jobs/{job_id}/seeds")
async def get_job_seeds(job_id: int, db: Database = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    job = db.get_job(job_id, owner_id=user.id, view_all=user.can("jobs.view_all"))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["source_type"] == "custom_urls":
        return db.get_job_custom_urls(job_id)

    # Resolve seeds from the frozen per-job snapshots, not the mutable catalog,
    # so the seed view survives a domains refresh. `id` is the original catalog
    # domain id (source_domain_id) so "Use same seeds" reposts domain_ids as
    # before; the displayed metadata comes from the frozen snapshot.
    return [
        {"id": s["source_domain_id"], "title": s["title"],
         "main_url": s["main_url"], "contact_url": s["contact_url"],
         "category_code": s["category_code"], "state": s["state"],
         "org_type": s["org_type"]}
        for s in db.get_crawl_snapshots(job_id)
    ]
