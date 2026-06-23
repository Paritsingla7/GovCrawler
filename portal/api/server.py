"""
FastAPI backend for the MishaCrawler Portal.

Endpoints:
  GET  /                          → serve frontend HTML
  GET  /api/categories            → list of {code, title, count}
  GET  /api/domains               → filtered + paginated domain list
  POST /api/import                → start background import from india.gov.in API
  GET  /api/import/status         → import progress
  POST /api/jobs                  → create + start a crawl job
  GET  /api/jobs                  → list recent jobs
  GET  /api/jobs/{id}             → single job status
  GET  /api/leads                 → paginated leads for a job
  GET  /api/leads/export          → CSV download
"""

import asyncio
import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright

from ..db.database import Database
from ..scraper.india_gov import import_all, import_status
from ..crawler.engine import CrawlerEngine

log = logging.getLogger(__name__)

# ── App state shared across requests ─────────────────────────────────────────
_db: Database | None = None
_config: dict | None = None
_browser = None
_playwright_instance = None

# job_id → asyncio.Task so we can check if it's still running
_active_tasks: dict[int, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _playwright_instance
    log.info("Starting Playwright browser...")
    _playwright_instance = await async_playwright().start()
    _browser = await _playwright_instance.chromium.launch(headless=True)
    log.info("Browser ready.")
    yield
    log.info("Shutting down browser...")
    try:
        await _browser.close()
        await _playwright_instance.stop()
    except Exception:
        pass


def create_app(config: dict, db: Database) -> FastAPI:
    global _db, _config
    _db = db
    _config = config

    app = FastAPI(title="MishaCrawler Portal", lifespan=lifespan)

    # ── Serve frontend ────────────────────────────────────────────────────────

    frontend_dir = Path(__file__).parent.parent / "frontend"

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_file = frontend_dir / "index.html"
        if not html_file.exists():
            return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
        return HTMLResponse(html_file.read_text(encoding="utf-8"))

    # ── Categories ────────────────────────────────────────────────────────────

    @app.get("/api/categories")
    async def get_categories():
        return _db.get_categories()

    # ── Domains ───────────────────────────────────────────────────────────────

    @app.get("/api/domains")
    async def get_domains(
        category: str = Query(None),
        search:   str = Query(None),
        page:     int = Query(1, ge=1),
        limit:    int = Query(50, ge=1, le=200),
    ):
        domains, total = _db.get_domains(
            category=category or None,
            search=search or None,
            page=page,
            limit=limit,
        )
        return {
            "domains": domains,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
        }

    # ── Import ────────────────────────────────────────────────────────────────

    @app.post("/api/import")
    async def trigger_import():
        if import_status.get("running"):
            return {"message": "Import already running", "status": import_status}
        asyncio.create_task(_run_import())
        return {"message": "Import started"}

    @app.get("/api/import/status")
    async def get_import_status():
        return import_status

    # ── Crawl jobs ────────────────────────────────────────────────────────────

    class StartJobRequest(BaseModel):
        domain_ids: list[int]
        category_filter: str | None = None
        title_filter: str | None = None

    @app.post("/api/jobs")
    async def create_job(req: StartJobRequest):
        if not req.domain_ids:
            raise HTTPException(status_code=400, detail="domain_ids is empty")

        # Validate IDs exist
        domains = _db.get_domains_by_ids(req.domain_ids)
        if not domains:
            raise HTTPException(status_code=404, detail="No matching domains found")

        job_id = _db.create_job(
            domain_ids=req.domain_ids,
            category_filter=req.category_filter,
            title_filter=req.title_filter,
        )

        # Build seed list: (contact_url or main_url, domain_id)
        seeds = []
        for d in domains:
            url = d["contact_url"] or d["main_url"]
            if url:
                seeds.append((url, d["id"]))

        if not seeds:
            _db.finish_job(job_id, status="failed", error="No valid URLs for selected domains")
            raise HTTPException(status_code=422, detail="Selected domains have no crawlable URLs")

        _db.start_job(job_id)
        task = asyncio.create_task(_run_crawl(job_id, seeds))
        _active_tasks[job_id] = task

        return {"id": job_id, "message": f"Crawl started for {len(seeds)} domains"}

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(20, ge=1, le=100)):
        return _db.list_jobs(limit=limit)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int):
        job = _db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        # Attach running state from in-memory task dict
        task = _active_tasks.get(job_id)
        if task and not task.done():
            job["status"] = "running"
        return job

    # ── Leads ─────────────────────────────────────────────────────────────────

    @app.get("/api/leads")
    async def get_leads(
        job_id: int = Query(...),
        page:   int = Query(1, ge=1),
        limit:  int = Query(100, ge=1, le=500),
    ):
        leads, total = _db.get_leads(job_id=job_id, page=page, limit=limit)
        return {
            "leads": leads,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }

    @app.get("/api/leads/export")
    async def export_leads(job_id: int = Query(...)):
        rows = _db.get_all_leads_for_export(job_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No leads for this job")

        output = io.StringIO()
        fieldnames = [
            "email", "phone", "person_name", "designation", "department",
            "domain_title", "category_title", "source_url", "context_snippet", "captured_at"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

        output.seek(0)
        filename = f"leads_job_{job_id}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


# ── Background tasks ──────────────────────────────────────────────────────────

async def _run_import():
    log.info("Background import started")
    await asyncio.to_thread(import_all, _db, _config)
    log.info("Background import finished")


async def _run_crawl(job_id: int, seeds: list[tuple[str, int | None]]):
    log.info(f"Crawl job {job_id} starting with {len(seeds)} seeds")
    try:
        engine = CrawlerEngine(config=_config, db=_db, job_id=job_id, browser=_browser)
        await engine.run(seeds)
        _db.finish_job(job_id, status="done")
        job = _db.get_job(job_id)
        log.info(f"Crawl job {job_id} done. Leads: {job['leads_found']}")
    except Exception as e:
        log.error(f"Crawl job {job_id} failed: {e}", exc_info=True)
        _db.finish_job(job_id, status="failed", error=str(e))
    finally:
        _active_tasks.pop(job_id, None)
