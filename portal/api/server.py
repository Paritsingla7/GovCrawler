"""
FastAPI backend for the GovCrawler Portal.

Endpoints:
  GET  /                    → frontend HTML
  GET  /api/categories      → [{code, title, count}]
  GET  /api/states          → state list (filtered by category if provided)
  GET  /api/org-types       → org type list (filtered by category+state)
  GET  /api/domains         → paginated domain list
  GET  /api/domains/ids     → all matching domain IDs (for select-all)
  GET  /api/config          → current crawler settings
  POST /api/config          → save crawler settings
  POST /api/import/json     → import from gov_domains.json (zero API calls)
  POST /api/import          → import from live india.gov.in API
  GET  /api/import/status   → import progress
  POST /api/jobs            → create + start a crawl job
  GET  /api/jobs            → list recent jobs
  GET  /api/jobs/{id}       → single job status
  GET  /api/leads           → paginated leads for a job
  GET  /api/leads/export    → CSV download
"""

import asyncio
import copy
import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel

from ..db.models import Database
from ..scraper.importer import import_all, import_from_json, import_status
from ..crawler.engine import CrawlerEngine

log = logging.getLogger(__name__)

_db: Database | None = None
_config: dict | None = None
_browser = None
_playwright_instance = None
_active_tasks: dict[int, asyncio.Task] = {}
_config_path: Path | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _playwright_instance
    log.info("Starting Playwright browser…")
    _playwright_instance = await async_playwright().start()
    _browser = await _playwright_instance.chromium.launch(headless=True)
    log.info("Browser ready.")
    yield
    log.info("Shutting down browser…")
    try:
        await _browser.close()
        await _playwright_instance.stop()
    except Exception:
        pass


def create_app(config: dict, db: Database) -> FastAPI:
    global _db, _config, _config_path
    _db = db
    _config = config
    _config_path = Path(__file__).parent.parent / "config.yaml"

    app = FastAPI(title="GovCrawler Portal", lifespan=lifespan)
    frontend_dir = Path(__file__).parent.parent / "frontend"

    # ── Frontend ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_file = frontend_dir / "index.html"
        if not html_file.exists():
            return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
        return HTMLResponse(html_file.read_text(encoding="utf-8"))

    @app.get("/leads", response_class=HTMLResponse)
    async def leads_page():
        html_file = frontend_dir / "leads.html"
        if not html_file.exists():
            return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)
        return HTMLResponse(html_file.read_text(encoding="utf-8"))

    @app.get("/api/logs")
    async def get_logs():
        log_file = Path("portal/data/portal.log")
        if not log_file.exists():
            return {"logs": "Log file not found."}
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-1000:]
            return {"logs": "".join(lines)}
        except Exception as e:
            return {"logs": f"Failed to read logs: {e}"}

    @app.delete("/api/visited-urls")
    async def clear_visited_urls():
        _db.clear_visited_urls()
        return {"message": "Visited URLs cleared."}

    # ── Metadata endpoints ────────────────────────────────────────────────────

    @app.get("/api/categories")
    async def get_categories():
        return _db.get_categories()

    @app.get("/api/states")
    async def get_states(category: str = Query(None)):
        return _db.get_states(category=category or None)

    @app.get("/api/org-types")
    async def get_org_types(category: str = Query(None), state: str = Query(None)):
        return _db.get_org_types(category=category or None, state=state or None)

    # ── Domains ───────────────────────────────────────────────────────────────

    @app.get("/api/domains")
    async def get_domains(
        category: str = Query(None),
        state:    str = Query(None),
        org_type: str = Query(None),
        search:   str = Query(None),
        page:     int = Query(1, ge=1),
        limit:    int = Query(50, ge=1, le=200),
    ):
        domains, total = _db.get_domains(
            category=category or None,
            state=state or None,
            org_type=org_type or None,
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

    @app.get("/api/domains/ids")
    async def get_domain_ids(
        category: str = Query(None),
        state:    str = Query(None),
        org_type: str = Query(None),
        search:   str = Query(None),
    ):
        ids = _db.get_domain_ids(
            category=category or None,
            state=state or None,
            org_type=org_type or None,
            search=search or None,
        )
        return {"ids": ids, "total": len(ids)}

    # ── Config ────────────────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config():
        c = _config
        return {
            "workers":              c["crawler"]["workers"],
            "max_depth":            c["crawler"]["max_depth"],
            "recrawl_days":         c["crawler"]["recrawl_days"],
            "request_delay":        c["crawler"]["request_delay"],
            "per_url_timeout":      c["crawler"]["per_url_timeout"],
            "httpx_first":          c["crawler"].get("httpx_first", True),
            "playwright_fallback":  c["crawler"].get("playwright_fallback", True),
            "playwright_timeout":   c["crawler"]["playwright_timeout"],
            "js_settle_time":       c["crawler"]["js_settle_time"],
            "email_enabled":        c["extraction"]["email"]["enabled"],
            "email_context_chars":  c["extraction"]["email"]["context_chars"],
            "person_enabled":       c["extraction"]["person"]["enabled"],
            "person_proximity_chars": c["extraction"]["person"]["proximity_chars"],
        }

    @app.post("/api/config")
    async def save_config(body: dict):
        cfg = copy.deepcopy(_config)

        int_keys   = {"workers", "max_depth", "recrawl_days", "per_url_timeout", "playwright_timeout"}
        float_keys = {"request_delay", "js_settle_time"}
        bool_keys  = {"httpx_first", "playwright_fallback"}

        for k in int_keys:
            if k in body:
                cfg["crawler"][k] = int(body[k])
        for k in float_keys:
            if k in body:
                cfg["crawler"][k] = float(body[k])
        for k in bool_keys:
            if k in body:
                cfg["crawler"][k] = bool(body[k])

        if "email_enabled" in body:
            cfg["extraction"]["email"]["enabled"] = bool(body["email_enabled"])
        if "email_context_chars" in body:
            cfg["extraction"]["email"]["context_chars"] = int(body["email_context_chars"])
        if "person_enabled" in body:
            cfg["extraction"]["person"]["enabled"] = bool(body["person_enabled"])
        if "person_proximity_chars" in body:
            cfg["extraction"]["person"]["proximity_chars"] = int(body["person_proximity_chars"])

        _config.update(cfg)

        with open(_config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return {"message": "Settings saved. Crawler settings take effect on the next job."}

    # ── Import ────────────────────────────────────────────────────────────────

    @app.post("/api/import/json")
    async def trigger_json_import(json_path: str = "gov_domains.json"):
        """Import domains from gov_domains.json — zero API calls."""
        if import_status.get("running"):
            return {"message": "Import already running", "status": import_status}
        asyncio.create_task(_run_json_import(json_path))
        return {"message": f"JSON import started from {json_path}"}

    @app.post("/api/import")
    async def trigger_import():
        """Import from live india.gov.in API — use only to refresh data."""
        if import_status.get("running"):
            return {"message": "Import already running", "status": import_status}
        asyncio.create_task(_run_import())
        return {"message": "API import started"}

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

        domains = _db.get_domains_by_ids(req.domain_ids)
        if not domains:
            raise HTTPException(status_code=404, detail="No matching domains found")

        job_id = _db.create_job(
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
            _db.finish_job(job_id, status="failed",
                           error="No valid URLs for selected domains")
            raise HTTPException(status_code=422,
                                detail="Selected domains have no crawlable URLs")

        _db.start_job(job_id)
        task = asyncio.create_task(_run_crawl(job_id, seeds))
        _active_tasks[job_id] = task

        return {"id": job_id,
                "message": f"Crawl started for {len(seeds)} domains"}

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(20, ge=1, le=100)):
        return _db.list_jobs(limit=limit)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int):
        job = _db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        task = _active_tasks.get(job_id)
        if task and not task.done():
            job["status"] = "running"
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        task = _active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            _db.finish_job(job_id, status="cancelled")
            return {"message": "Job cancelled"}
        return {"message": "Job is not currently running"}

    # ── Leads ─────────────────────────────────────────────────────────────────

    @app.get("/api/leads")
    async def get_leads(
        job_id: int = Query(None),
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
    async def export_leads(job_id: int = Query(None)):
        rows = _db.get_all_leads_for_export(job_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No leads for this job")

        output = io.StringIO()
        fieldnames = [
            "email", "person_name", "designation", "department",
            "domain_title", "domain_state", "domain_org_type",
            "category_title", "source_url", "context_snippet", "captured_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="leads_job_{job_id}.csv"'},
        )

    return app


# ── Background tasks ──────────────────────────────────────────────────────────

async def _run_json_import(json_path: str):
    log.info(f"Background JSON import started from {json_path}")
    await asyncio.to_thread(import_from_json, _db, json_path, _config)
    log.info("Background JSON import finished")


async def _run_import():
    log.info("Background API import started")
    await asyncio.to_thread(import_all, _db, _config)
    log.info("Background API import finished")


async def _run_crawl(job_id: int, seeds: list[tuple[str, int | None]]):
    log.info(f"Crawl job {job_id} starting with {len(seeds)} seeds")
    try:
        engine = CrawlerEngine(config=_config, db=_db,
                               job_id=job_id, browser=_browser)
        await engine.run(seeds)
        _db.finish_job(job_id, status="done")
        job = _db.get_job(job_id)
        log.info(f"Crawl job {job_id} done. Leads: {job['leads_found']}")
    except Exception as e:
        log.error(f"Crawl job {job_id} failed: {e}", exc_info=True)
        _db.finish_job(job_id, status="failed", error=str(e))
    finally:
        _active_tasks.pop(job_id, None)
