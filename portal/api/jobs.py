"""
Crawl job endpoints.

Registers routes:
  POST /api/jobs                 → create + start a crawl job
  GET  /api/jobs                 → list recent jobs
  GET  /api/jobs/{id}            → single job status
  GET  /api/jobs/{id}/seeds      → resolve a job's seed domains
  POST /api/jobs/{id}/cancel     → cancel a running job
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..crawler.engine import CrawlerEngine
from ..db import CrawlJob, Database
from .deps import get_active_tasks, get_browser, get_config as get_app_config, get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


class StartJobRequest(BaseModel):
    domain_ids: list[int]
    category_filter: str | None = None
    title_filter: str | None = None


async def _run_crawl(job_id: int, seeds: list[tuple[str, int | None]],
                     db: Database, config: dict, browser,
                     active_tasks: dict[int, asyncio.Task]):
    log.info(f"Crawl job {job_id} starting with {len(seeds)} seeds")
    try:
        engine = CrawlerEngine(config=config, db=db, job_id=job_id, browser=browser)
        await engine.run(seeds)
        db.finish_job(job_id, status="done")
        job = db.get_job(job_id)
        log.info(f"Crawl job {job_id} done. Leads: {job['leads_found']}")
    except asyncio.CancelledError:
        log.info(f"Crawl job {job_id} cancelled by user.")
        raise
    except Exception as e:
        log.error(f"Crawl job {job_id} failed: {e}", exc_info=True)
        db.finish_job(job_id, status="failed", error=str(e))
    finally:
        active_tasks.pop(job_id, None)


@router.post("/api/jobs")
async def create_job(
        req: StartJobRequest,
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        browser=Depends(get_browser),
        active_tasks: dict = Depends(get_active_tasks),
):
    if not req.domain_ids:
        raise HTTPException(status_code=400, detail="domain_ids is empty")

    domains = db.get_domains_by_ids(req.domain_ids)
    if not domains:
        raise HTTPException(status_code=404, detail="No matching domains found")

    job_id = db.create_job(
        domain_ids=req.domain_ids,
        category_filter=req.category_filter,
        title_filter=req.title_filter,
    )

    seeds = []
    for d in domains:
        url = d["contact_url"] or d["main_url"]
        if url:
            seeds.append((url, d["id"]))

    if not seeds:
        db.finish_job(job_id, status="failed",
                      error="No valid URLs for selected domains")
        raise HTTPException(status_code=422,
                            detail="Selected domains have no crawlable URLs")

    db.start_job(job_id)
    task = asyncio.create_task(_run_crawl(job_id, seeds, db, config, browser, active_tasks))
    active_tasks[job_id] = task

    return {"id": job_id,
            "message": f"Crawl started for {len(seeds)} domains"}


@router.get("/api/jobs")
async def list_jobs(limit: int = Query(20, ge=1, le=100), db: Database = Depends(get_db)):
    return db.list_jobs(limit=limit)


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: int, db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    task = active_tasks.get(job_id)
    if task and not task.done():
        job["status"] = "running"
    return job


@router.get("/api/jobs/{job_id}/seeds")
async def get_job_seeds(job_id: int, db: Database = Depends(get_db)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # In CrawlJob, domain_ids is stored as JSON list[int].
    # Since db.get_job doesn't include it in _job_dict, we can fetch it directly from the DB here
    with db._Session() as s:
        j = s.query(CrawlJob).filter_by(id=job_id).first()
        if not j or not j.domain_ids:
            return []
        try:
            ids = json.loads(j.domain_ids)
            return db.get_domains_by_ids(ids)
        except Exception:
            return []


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, db: Database = Depends(get_db), active_tasks: dict = Depends(get_active_tasks)):
    task = active_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        db.finish_job(job_id, status="cancelled")
        return {"message": "Job cancelled"}
    return {"message": "Job is not currently running"}
