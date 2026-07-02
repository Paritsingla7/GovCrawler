"""
FastAPI app factory for the GovCrawler Portal.

Route definitions live in per-concern modules (frontend, domains, config,
imports, jobs, leads, templates, blacklist, campaigns, credentials); this
module only builds the FastAPI app, mounts static files, manages the
Playwright browser lifespan, and wires shared state into portal.api.deps.

See each route module's docstring for its endpoint list.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

from ..db import Database
from . import blacklist, campaigns, config, credentials, deps, domains, frontend, imports, jobs, leads, templates

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Playwright browser…")
    deps._playwright_instance = await async_playwright().start()
    deps._browser = await deps._playwright_instance.chromium.launch(headless=True)
    log.info("Browser ready.")
    yield
    log.info("Shutting down browser…")
    try:
        await deps._browser.close()
        await deps._playwright_instance.stop()
    except Exception:
        pass


def create_app(config_dict: dict, db: Database) -> FastAPI:
    deps._db = db
    deps._config = config_dict
    deps._config_path = Path(__file__).parent.parent / "config.yaml"

    app = FastAPI(title="GovCrawler Portal", lifespan=lifespan)

    # Mount static files
    static_dir = Path(__file__).parent.parent / "frontend" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(frontend.router)
    app.include_router(domains.router)
    app.include_router(config.router)
    app.include_router(imports.router)
    app.include_router(jobs.router)
    app.include_router(leads.router)
    app.include_router(templates.router)
    app.include_router(blacklist.router)
    app.include_router(campaigns.router)
    app.include_router(credentials.router)

    return app
