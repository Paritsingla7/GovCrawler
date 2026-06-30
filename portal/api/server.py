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
  POST /api/import/json     → import from uploaded JSON file (zero API calls)
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
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from playwright.async_api import async_playwright
from pydantic import BaseModel

from ..crawler.engine import CrawlerEngine
from ..db.models import Database
from ..scraper.importer import import_all, import_from_json, import_status

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


from fastapi.staticfiles import StaticFiles


def create_app(config: dict, db: Database) -> FastAPI:
    global _db, _config, _config_path
    _db = db
    _config = config
    _config_path = Path(__file__).parent.parent / "config.yaml"

    app = FastAPI(title="GovCrawler Portal", lifespan=lifespan)
    frontend_dir = Path(__file__).parent.parent / "frontend"

    # Mount static files
    static_dir = frontend_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(frontend_dir))

    # ── Frontend ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        template = templates.get_template("index.html")
        return HTMLResponse(template.render({"request": request}))

    @app.get("/leads", response_class=HTMLResponse)
    async def leads_page(request: Request):
        template = templates.get_template("leads.html")
        return HTMLResponse(template.render({"request": request}))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        template = templates.get_template("settings.html")
        return HTMLResponse(template.render({"request": request}))

    @app.get("/test-campaign", response_class=HTMLResponse)
    async def test_campaign_page(request: Request):
        template = templates.get_template("test-campaign.html")
        return HTMLResponse(template.render({"request": request}))

    @app.get("/campaigns", response_class=HTMLResponse)
    async def campaigns_page(request: Request):
        template = templates.get_template("campaigns.html")
        return HTMLResponse(template.render({"request": request}))

    @app.get("/user-guide", response_class=HTMLResponse)
    async def user_guide_page(request: Request):
        template = templates.get_template("user-guide.html")
        return HTMLResponse(template.render({"request": request}))

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
            state: str = Query(None),
            org_type: str = Query(None),
            search: str = Query(None),
            page: int = Query(1, ge=1),
            limit: int = Query(50, ge=1, le=200),
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
            state: str = Query(None),
            org_type: str = Query(None),
            search: str = Query(None),
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
            "workers": c["crawler"]["workers"],
            "max_depth": c["crawler"]["max_depth"],
            "recrawl_days": c["crawler"]["recrawl_days"],
            "request_delay": c["crawler"]["request_delay"],
            "per_url_timeout": c["crawler"]["per_url_timeout"],
            "httpx_first": c["crawler"].get("httpx_first", True),
            "playwright_fallback": c["crawler"].get("playwright_fallback", True),
            "playwright_timeout": c["crawler"]["playwright_timeout"],
            "js_settle_time": c["crawler"]["js_settle_time"],
            "email_enabled": c["extraction"]["email"]["enabled"],
            "email_context_chars": c["extraction"]["email"]["context_chars"],
            "person_enabled": c["extraction"]["person"]["enabled"],
            "person_proximity_chars": c["extraction"]["person"]["proximity_chars"],

            # Arrays
            "target_suffixes": "\n".join(c["crawler"].get("target_suffixes", [])),
            "priority_keywords": "\n".join(c["crawler"].get("priority_keywords", [])),
            "skip_extensions": "\n".join(c["crawler"].get("skip_extensions", [])),
            "valid_suffixes": "\n".join(c["extraction"]["email"].get("valid_suffixes", [])),
            "title_prefixes": "\n".join(c["extraction"]["person"].get("title_prefixes", [])),
            "designation_keywords": "\n".join(c["extraction"]["person"].get("designation_keywords", [])),

            # Dictionary
            "max_links_per_page_0": c["crawler"].get("max_links_per_page", {}).get(0, 30),
            "max_links_per_page_1": c["crawler"].get("max_links_per_page", {}).get(1, 15),
            "max_links_per_page_2": c["crawler"].get("max_links_per_page", {}).get(2, 8),
            "max_links_per_page_default": c["crawler"].get("max_links_per_page", {}).get("default", 5),

            # Read-only
            "user_agent": c["crawler"].get("user_agent", ""),
            "js_indicators": "\n".join(c["crawler"].get("js_indicators", [])),
            "email_regex": c["extraction"]["email"].get("regex", ""),
            "email_obfuscation": yaml.dump(c["extraction"]["email"].get("obfuscation", []), default_flow_style=False),
        }

    @app.post("/api/config")
    async def save_config(body: dict):
        cfg = copy.deepcopy(_config)

        int_keys = {"workers", "max_depth", "recrawl_days", "per_url_timeout", "playwright_timeout"}
        float_keys = {"request_delay", "js_settle_time"}
        bool_keys = {"httpx_first", "playwright_fallback"}

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

        def parse_list(s: str) -> list[str]:
            return [x.strip() for x in s.replace(",", "\n").split("\n") if x.strip()]

        if "target_suffixes" in body:
            cfg["crawler"]["target_suffixes"] = parse_list(body["target_suffixes"])
        if "priority_keywords" in body:
            cfg["crawler"]["priority_keywords"] = parse_list(body["priority_keywords"])
        if "skip_extensions" in body:
            cfg["crawler"]["skip_extensions"] = parse_list(body["skip_extensions"])
        if "valid_suffixes" in body:
            cfg["extraction"]["email"]["valid_suffixes"] = parse_list(body["valid_suffixes"])
        if "title_prefixes" in body:
            cfg["extraction"]["person"]["title_prefixes"] = parse_list(body["title_prefixes"])
        if "designation_keywords" in body:
            cfg["extraction"]["person"]["designation_keywords"] = parse_list(body["designation_keywords"])

        # dict updates
        max_links = cfg["crawler"].setdefault("max_links_per_page", {})
        if "max_links_per_page_0" in body:
            max_links[0] = int(body["max_links_per_page_0"])
        if "max_links_per_page_1" in body:
            max_links[1] = int(body["max_links_per_page_1"])
        if "max_links_per_page_2" in body:
            max_links[2] = int(body["max_links_per_page_2"])
        if "max_links_per_page_default" in body:
            max_links["default"] = int(body["max_links_per_page_default"])

        _config.update(cfg)

        with open(_config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return {"message": "Settings saved. Crawler settings take effect on the next job."}

    # ── Import ────────────────────────────────────────────────────────────────

    @app.post("/api/import/json")
    async def trigger_json_import(file: UploadFile = File(...)):
        """Import domains from an uploaded JSON file — zero API calls."""
        if import_status.get("running"):
            return {"message": "Import already running", "status": import_status}
        content = await file.read()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp.write(content)
        tmp.close()
        asyncio.create_task(_run_json_import(tmp.name, cleanup=True))
        return {"message": f"JSON import started from {file.filename}"}

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

    @app.get("/api/jobs/{job_id}/seeds")
    async def get_job_seeds(job_id: int):
        import json
        job = _db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        # In CrawlJob, domain_ids is stored as JSON list[int].
        # Since _db.get_job doesn't include it in _job_dict, we can fetch it directly from the DB here
        with _db._Session() as s:
            from ..db.models import CrawlJob
            j = s.query(CrawlJob).filter_by(id=job_id).first()
            if not j or not j.domain_ids:
                return []
            try:
                ids = json.loads(j.domain_ids)
                return _db.get_domains_by_ids(ids)
            except Exception:
                return []

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
            category: str = Query(None),
            state: str = Query(None),
            search: str = Query(None),
            complete_only: bool = Query(False),
            page: int = Query(1, ge=1),
            limit: int = Query(100, ge=1, le=500),
    ):
        leads, total = _db.get_leads(job_id=job_id, category=category, state=state, search=search,
                                     complete_only=complete_only, page=page, limit=limit)
        return {
            "leads": leads,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }

    @app.get("/api/leads/ids")
    async def get_lead_ids(
            job_id: int = Query(None),
            category: str = Query(None),
            state: str = Query(None),
            search: str = Query(None),
            complete_only: bool = Query(False),
    ):
        ids = _db.get_lead_ids(job_id=job_id, category=category, state=state, search=search,
                               complete_only=complete_only)
        return {"ids": ids, "total": len(ids)}

    @app.get("/api/leads/categories")
    async def get_lead_categories(job_id: int = Query(None)):
        return _db.get_lead_categories(job_id=job_id)

    @app.get("/api/leads/states")
    async def get_lead_states(job_id: int = Query(None), category: str = Query(None)):
        return _db.get_lead_states(job_id=job_id, category=category)

    _ALL_EXPORT_FIELDS = [
        "email", "person_name", "designation", "department",
        "domain_title", "domain_state", "domain_org_type",
        "category_title", "source_url", "source_title", "context_snippet",
        "depth", "captured_at",
    ]

    class ExportLeadsRequest(BaseModel):
        job_id: int | None = None
        category: str | None = None
        state: str | None = None
        search: str | None = None
        complete_only: bool = False
        lead_ids: list[int] | None = None
        fields: list[str] | None = None

    class LeadUpdate(BaseModel):
        person_name: str | None = None
        designation: str | None = None
        department: str | None = None
        domain_state: str | None = None

    @app.post("/api/leads/export")
    async def export_leads(req: ExportLeadsRequest):
        rows = _db.get_all_leads_for_export(
            job_id=req.job_id,
            category=req.category,
            state=req.state,
            search=req.search,
            lead_ids=req.lead_ids,
            complete_only=req.complete_only,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="No leads for this job")

        # Keep only the requested fields (email always included), preserving canonical order
        if req.fields:
            allowed = set(req.fields) | {"email"}
            fieldnames = [f for f in _ALL_EXPORT_FIELDS if f in allowed]
        else:
            fieldnames = _ALL_EXPORT_FIELDS

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition":
                         f'attachment; filename="leads_export.csv"'},
        )

    @app.put("/api/leads/{lead_id}")
    async def update_lead(lead_id: int, req: LeadUpdate):
        updates = req.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        ok = _db.update_lead(lead_id, updates)
        if not ok:
            raise HTTPException(status_code=404, detail="Lead not found")
        return {"ok": True}

    # ── Outreach & Campaign routes (Phase 2) ──────────────────────────────────
    from .templates import register_template_routes
    from .blacklist import register_blacklist_routes
    from .campaigns import register_campaign_routes
    from .credentials import register_credential_routes

    register_template_routes(app, db)
    register_blacklist_routes(app, db)
    register_campaign_routes(app, db)
    register_credential_routes(app, db)

    return app


# ── Background tasks ──────────────────────────────────────────────────────────

async def _run_json_import(json_path: str, cleanup: bool = False):
    log.info(f"Background JSON import started from {json_path}")
    try:
        await asyncio.to_thread(import_from_json, _db, json_path, _config)
    finally:
        if cleanup:
            Path(json_path).unlink(missing_ok=True)
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
    except asyncio.CancelledError:
        log.info(f"Crawl job {job_id} cancelled by user.")
        raise
    except Exception as e:
        log.error(f"Crawl job {job_id} failed: {e}", exc_info=True)
        _db.finish_job(job_id, status="failed", error=str(e))
    finally:
        _active_tasks.pop(job_id, None)
