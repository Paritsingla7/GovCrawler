"""Coordination endpoints — the agent↔cloud contract a CloudApiClient speaks
(not the browser). Routes and the durability model are in .docs/api-reference.md
and .docs/resilience.md."""

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from shared.urls import strip_www

from .deps import CurrentUser, forbid_unless_owner, get_config as get_app_config, get_current_user, get_db
from ..db import Database

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/coordination", tags=["coordination"])


def _authorized_job(db: Database, job_id: int, user: CurrentUser, *, allow: str | None = None) -> dict:
    """Fetch a job for a coordination write, 404 if missing, and enforce
    ownership (owner or admin; `allow` widens it, e.g. crawl.cancel_all for
    cancel). Job writes authorize on ownership, not the volatile crawl.run
    grant, so revoking a permission mid-crawl can't strand the outbox
    (plan.md §16.2)."""
    job = db.get_job(job_id, view_all=True)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    forbid_unless_owner(job["owner_id"], user, allow=allow)
    return job


def _seed_root_domains(seeds: list[tuple[str, int | None]]) -> set[str]:
    roots = set()
    for url, _ in seeds:
        parsed = url if "://" in url else "http://" + url
        roots.add(strip_www(urlsplit(parsed).netloc.lower()))
    return roots


def _build_crawl_policy(config: dict, db: Database, *, strip_target_suffixes: bool = False) -> dict:
    """The dict handed to CrawlerEngine as its `config` — same shape it always
    had (top-level `crawler`/`extraction` keys), just assembled from two
    sources instead of one (plan.md §19.1 Phase 8 / §3.2): machine-local
    runtime knobs from this box's config.yaml, crawl policy (depth/rate
    limits, filters, extraction rules, lead-score weights) from the cloud
    `app_settings` table — the DB values win on any overlapping `crawler` key
    so every crawler gets identical policy regardless of its local file."""
    stored = db.get_crawl_policy()
    crawler = {**config["crawler"], **stored.get("crawler", {})}
    if strip_target_suffixes:
        crawler["target_suffixes"] = []
    return {
        **config,
        "crawler": crawler,
        "extraction": stored.get("extraction") or config.get("extraction", {}),
        "lead_score": {"weights": db._lead_score_weights},
    }


def _visited_bootstrap(db: Database, job_id: int, seeds: list[tuple[str, int | None]]) -> list[str]:
    """Everything this job's engine should treat as already-visited: its own
    prior-run URLs (resume) plus the global recently-visited set, EXCLUDING
    URLs whose root domain belongs to one of THIS job's seeds (those must
    stay freely re-crawlable regardless of when they were last visited)."""
    seed_roots = _seed_root_domains(seeds)
    bootstrap = set(db.get_visited_urls(job_id))
    for url in db.get_recently_visited_global():
        root = strip_www(urlsplit(url).netloc.lower())
        is_seed_related = any(root == r or root.endswith("." + r) for r in seed_roots)
        if not is_seed_related:
            bootstrap.add(url)
    return list(bootstrap)


class CoordinationJobCreate(BaseModel):
    domain_ids: list[int] | None = None
    custom_urls: list[str] | None = None
    category_filter: str | None = None
    title_filter: str | None = None


@router.post("/jobs")
async def coordination_create_job(
        req: CoordinationJobCreate,
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        user: CurrentUser = Depends(get_current_user),
):
    from .jobs import _normalize_custom_urls  # local import: avoids a jobs<->coordination import cycle

    if req.custom_urls:
        policy = _build_crawl_policy(config, db, strip_target_suffixes=True)
        max_urls = policy["crawler"].get("max_custom_urls", 50)
        urls = _normalize_custom_urls(req.custom_urls, max_urls)
        job_id = db.create_job(custom_urls=urls, category_filter=req.category_filter,
                               title_filter=req.title_filter, owner_id=user.id)
        db.add_job_custom_urls(job_id, urls)
        seeds = [[url, None] for url in urls]
    else:
        if not req.domain_ids:
            raise HTTPException(status_code=422, detail="Provide domain_ids or custom_urls")
        domains = db.get_domains_by_ids(req.domain_ids)
        if not domains:
            raise HTTPException(status_code=404, detail="No matching domains found")

        job_id = db.create_job(domain_ids=req.domain_ids, category_filter=req.category_filter,
                               title_filter=req.title_filter, owner_id=user.id)
        seeds = []
        for d in domains:
            url = d["contact_url"] or d["main_url"]
            if url:
                snap_id = db.create_crawl_snapshot(job_id, d)
                seeds.append([url, snap_id])

        if not seeds:
            db.finish_job(job_id, status="failed", error="No valid URLs for selected domains")
            raise HTTPException(status_code=422, detail="Selected domains have no crawlable URLs")
        policy = _build_crawl_policy(config, db)

    db.start_job(job_id)
    visited_bootstrap = _visited_bootstrap(db, job_id, [(s[0], s[1]) for s in seeds])
    return {"job_id": job_id, "seeds": seeds, "policy": policy, "visited_bootstrap": visited_bootstrap}


class LeadBatch(BaseModel):
    items: list[dict]


@router.post("/jobs/{job_id}/leads")
async def coordination_save_leads(
        job_id: int, batch: LeadBatch,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    for item in batch.items:
        item["job_id"] = job_id
    results = db.bulk_save_leads(batch.items, captured_by=user.id)
    return {"accepted": sum(1 for r in results if r), "total": len(results)}


class VisitedBatch(BaseModel):
    urls: list[str]


@router.post("/jobs/{job_id}/visited")
async def coordination_mark_visited(
        job_id: int, batch: VisitedBatch,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    db.bulk_mark_visited(job_id, batch.urls)
    return {"marked": len(batch.urls)}


class HeartbeatPayload(BaseModel):
    queued_urls: int = 0
    visited_urls: int = 0
    skipped_urls: int = 0
    leads_found: int = 0
    crawled_domains: int = 0
    current_depth: int = 0
    active_workers: int = 0


@router.post("/jobs/{job_id}/heartbeat")
async def coordination_heartbeat(
        job_id: int, metrics: HeartbeatPayload,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    cancel_requested = db.heartbeat(job_id, metrics.model_dump())
    return {"cancel_requested": cancel_requested}


class FrontierPayload(BaseModel):
    snapshot: dict


@router.post("/jobs/{job_id}/frontier")
async def coordination_save_frontier(
        job_id: int, payload: FrontierPayload,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    db.save_frontier_snapshot(job_id, payload.snapshot)
    return {"status": "ok"}


@router.get("/jobs/{job_id}/frontier")
async def coordination_load_frontier(
        job_id: int,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    snapshot = db.load_frontier_snapshot(job_id)
    return {"snapshot": snapshot}


class FinishPayload(BaseModel):
    status: str = "done"
    error: str | None = None


@router.post("/jobs/{job_id}/finish")
async def coordination_finish_job(
        job_id: int, payload: FinishPayload,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    _authorized_job(db, job_id, user)
    db.finish_job(job_id, status=payload.status, error=payload.error)
    return {"status": "ok"}


@router.post("/jobs/{job_id}/cancel")
async def coordination_cancel_job(
        job_id: int,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(get_current_user),
):
    """Sets the cancel signal only — does NOT flip status to 'cancelled'
    itself. Whoever is actually running the engine (this machine's agent
    today, a real remote one later) is responsible for calling `finish`
    once it has actually stopped and drained its outbox."""
    _authorized_job(db, job_id, user, allow="crawl.cancel_all")
    db.set_cancel_requested(job_id)
    return {"cancel_requested": True}


@router.post("/jobs/{job_id}/resume")
async def coordination_resume_job(
        job_id: int,
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        user: CurrentUser = Depends(get_current_user),
):
    job = _authorized_job(db, job_id, user)
    db.resume_job(job_id)
    if job["source_type"] == "custom_urls":
        seeds = [[c["main_url"], None] for c in db.get_job_custom_urls(job_id)]
    else:
        snaps = db.get_crawl_snapshots(job_id)
        if not snaps:
            # Pre-snapshot-feature job: build them now from the catalog
            # (get-or-insert, idempotent), then re-read.
            for d in db.get_domains_by_ids(db.get_job_domain_ids(job_id)):
                if d["contact_url"] or d["main_url"]:
                    db.create_crawl_snapshot(job_id, d)
            snaps = db.get_crawl_snapshots(job_id)
        seeds = [[s["main_url"] or s["contact_url"], s["id"]] for s in snaps]
    visited_bootstrap = _visited_bootstrap(db, job_id, [(s[0], s[1]) for s in seeds])
    policy = _build_crawl_policy(config, db)
    return {"job_id": job_id, "seeds": seeds, "policy": policy, "visited_bootstrap": visited_bootstrap}
