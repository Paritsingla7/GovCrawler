"""
FastAPI app factory for the GovCrawler Portal.

Route definitions live in per-concern modules (frontend, domains, config,
imports, jobs, leads, templates, blacklist, campaigns, credentials); this
module only builds the FastAPI app, mounts static files, manages the
Playwright browser lifespan, and wires shared state into portal.api.deps.

See each route module's docstring for its endpoint list.
"""

import logging
import secrets
import yaml
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from playwright.async_api import async_playwright

from . import (
    admin, auth, blacklist, campaigns, config, credentials, deps, domains, frontend, imports, jobs, leads, system,
    templates,
)
from .deps import RedirectException, get_current_user
from ..db import Database

log = logging.getLogger(__name__)


def _ensure_jwt_secret(config_dict: dict, config_path: Path) -> None:
    """Generate + persist a random JWT secret on first run so it survives restarts."""
    if config_dict.get("auth", {}).get("jwt_secret"):
        return
    config_dict.setdefault("auth", {})["jwt_secret"] = secrets.token_urlsafe(48)
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("Generated and persisted a new auth.jwt_secret")


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

    _ensure_jwt_secret(config_dict, deps._config_path)

    app = FastAPI(title="GovCrawler Portal", lifespan=lifespan)

    @app.exception_handler(RedirectException)
    async def _redirect_handler(request: Request, exc: RedirectException):
        return RedirectResponse(url=exc.location, status_code=302)

    # Mount static files
    static_dir = Path(__file__).parent.parent / "frontend" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(frontend.router)
    app.include_router(domains.router, dependencies=[Depends(get_current_user)])
    app.include_router(config.router, dependencies=[Depends(get_current_user)])
    app.include_router(imports.router, dependencies=[Depends(get_current_user)])
    app.include_router(jobs.router, dependencies=[Depends(get_current_user)])
    app.include_router(leads.router, dependencies=[Depends(get_current_user)])
    app.include_router(templates.router, dependencies=[Depends(get_current_user)])
    app.include_router(blacklist.router, dependencies=[Depends(get_current_user)])
    app.include_router(campaigns.router, dependencies=[Depends(get_current_user)])
    app.include_router(credentials.router, dependencies=[Depends(get_current_user)])
    app.include_router(system.router)

    return app
