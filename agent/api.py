"""Agent-tier BFF routes that own a running crawl (create / resume / cancel).

These talk to the cloud over the coordination HTTP contract via cloud_client.py.
Known, flagged exception: `_make_token_provider` and a few `cloud.*` imports
still touch the cloud process directly because the agent isn't a separate
process yet. See .docs/architecture.md ("Deployment reality vs. target").
"""

import asyncio
import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator

from .cloud_client import CloudApiClient, create_remote_job, resume_remote_job
from .crawler.engine import CrawlerEngine
from cloud.api.deps import CurrentUser, get_active_tasks, get_browser, get_config as get_app_config, get_db, require
from cloud.db import Database
from cloud.security.jwt import create_access_token
from portal.paths import DATA_DIR

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


class StartJobRequest(BaseModel):
    domain_ids: list[int] | None = None
    custom_urls: list[str] | None = None
    category_filter: str | None = None
    title_filter: str | None = None

    @model_validator(mode="after")
    def _check_exclusive_source(self):
        if bool(self.domain_ids) == bool(self.custom_urls):
            raise ValueError("Provide exactly one of domain_ids or custom_urls")
        return self


def _cloud_base_url(config: dict) -> str:
    """Defaults to this same process's own loopback address — today's single-
    box deployment talks to itself over HTTP instead of a real second
    machine. Override via config['cloud_api_base_url'] once a real VPS split
    exists (plan.md §6)."""
    if config.get("cloud_api_base_url"):
        return config["cloud_api_base_url"]
    port = config.get("api", {}).get("port", 8001)
    return f"http://127.0.0.1:{port}"


def _make_token_provider(db: Database, config: dict, user: CurrentUser):
    """Mints a fresh access token per call instead of caching one — a crawl
    can run far longer than one token's TTL, and re-encoding a JWT is cheap
    (no DB round trip), so there's nothing to refresh. See module docstring
    for why this is the one DB touchpoint left in the agent tier."""
    secret = config["auth"]["jwt_secret"]
    ttl = config["auth"].get("access_ttl_minutes", 15)
    token_version = db.get_user_by_id(user.id)["token_version"]
    return lambda: create_access_token(user.id, token_version, secret, ttl)


async def _run_crawl(job_id: int, seeds: list[tuple[str, int | None]], visited_bootstrap: list[str],
                     cloud: CloudApiClient, config: dict, browser,
                     active_tasks: dict[int, asyncio.Task], frontier: dict | None = None):
    log.info(f"Crawl job {job_id} starting with {len(seeds)} seeds"
            + (" (resumed from checkpoint)" if frontier else ""))
    cloud.start()
    try:
        engine = CrawlerEngine(config=config, cloud=cloud, job_id=job_id, browser=browser)
        await engine.run(seeds, visited_bootstrap=visited_bootstrap, frontier=frontier)
        await cloud.finish_job(status="done")
        cloud.clear_frontier()
        log.info(f"Crawl job {job_id} done.")
    except asyncio.CancelledError:
        log.info(f"Crawl job {job_id} cancelled.")
        await cloud.best_effort_drain()
        await cloud.finish_job(status="cancelled")
        raise
    except Exception as e:
        log.error(f"Crawl job {job_id} failed: {e}", exc_info=True)
        await cloud.finish_job(status="failed", error=str(e))
    finally:
        active_tasks.pop(job_id, None)
        await cloud.aclose()


@router.post("/api/jobs")
async def create_job(
        req: StartJobRequest,
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        browser=Depends(get_browser),
        active_tasks: dict = Depends(get_active_tasks),
        user: CurrentUser = Depends(require("crawl.run")),
):
    base_url = _cloud_base_url(config)
    token_provider = _make_token_provider(db, config, user)

    body = {"domain_ids": req.domain_ids, "custom_urls": req.custom_urls,
           "category_filter": req.category_filter, "title_filter": req.title_filter}
    try:
        created = await create_remote_job(base_url, token_provider, **body)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    job_id = created["job_id"]
    seeds = [(s[0], s[1]) for s in created["seeds"]]
    engine_config = created["policy"]

    outbox_path = DATA_DIR / f"outbox_job_{job_id}.db"
    cross_machine_resume = engine_config.get("crawler", {}).get("cross_machine_resume", False)
    cloud = CloudApiClient(base_url, token_provider, job_id, outbox_path,
                           cross_machine_resume=cross_machine_resume)
    task = asyncio.create_task(_run_crawl(job_id, seeds, created["visited_bootstrap"],
                                          cloud, engine_config, browser, active_tasks))
    active_tasks[job_id] = task

    return {"id": job_id,
            "message": f"Crawl started for {len(seeds)} seed URL(s)"}


@router.post("/api/jobs/{job_id}/resume")
async def resume_job(
        job_id: int,
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        browser=Depends(get_browser),
        active_tasks: dict = Depends(get_active_tasks),
        user: CurrentUser = Depends(require("crawl.run")),
):
    """Manual resume of an `interrupted` job — reloads the local frontier
    checkpoint (if this machine has one) and continues the queue instead of
    re-crawling from seeds. Automatic resume-on-process-restart (scanning for
    orphaned frontier files at startup) is a follow-up, not attempted here."""
    if job_id in active_tasks and not active_tasks[job_id].done():
        raise HTTPException(status_code=409, detail="Job is already running")

    base_url = _cloud_base_url(config)
    token_provider = _make_token_provider(db, config, user)

    try:
        resumed = await resume_remote_job(base_url, token_provider, job_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    outbox_path = DATA_DIR / f"outbox_job_{job_id}.db"
    cross_machine_resume = resumed["policy"].get("crawler", {}).get("cross_machine_resume", False)
    cloud = CloudApiClient(base_url, token_provider, job_id, outbox_path,
                           cross_machine_resume=cross_machine_resume)
    frontier = await cloud.load_frontier()
    if frontier is None:
        log.warning(f"Job {job_id}: no local frontier checkpoint found — resuming from seeds instead")

    seeds = [(s[0], s[1]) for s in resumed["seeds"]]
    task = asyncio.create_task(_run_crawl(job_id, seeds, resumed["visited_bootstrap"],
                                          cloud, resumed["policy"], browser, active_tasks,
                                          frontier=frontier))
    active_tasks[job_id] = task

    return {"id": job_id,
            "message": "Crawl resumed" + (" from checkpoint" if frontier else " from seeds (no checkpoint found)")}


def cancel_job_if_running(job_id: int, active_tasks: dict[int, asyncio.Task]) -> bool:
    """Cancels this machine's local task if it's the one running the job.
    Returns whether it was running HERE — a real remote agent's job would
    return False (nothing local to cancel), relying entirely on the
    coordination cancel_requested flag being seen on its next heartbeat."""
    task = active_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, db: Database = Depends(get_db), config: dict = Depends(get_app_config),
                     active_tasks: dict = Depends(get_active_tasks),
                     user: CurrentUser = Depends(require("crawl.run"))):
    base_url = _cloud_base_url(config)
    token_provider = _make_token_provider(db, config, user)
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
        r = await http.post(f"/api/coordination/jobs/{job_id}/cancel",
                            headers={"Authorization": f"Bearer {token_provider()}"})
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Job not found")
    if r.status_code == 403:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    r.raise_for_status()

    if cancel_job_if_running(job_id, active_tasks):
        return {"message": "Job cancelled"}
    return {"message": "Cancellation requested — the owning agent will stop it on its next heartbeat"}
