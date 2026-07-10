"""FastAPI app factory. Builds the app, mounts the per-concern routers, manages
the reaper lifespan, and wires shared state into cloud.api.deps. The cloud
tier is genuinely crawler-free (plan.md §19.1 Phase 9 Part 2, 2.3) — it never
imports `agent.*` and never touches Playwright; the agent's own standalone
BFF (agent/bff/app.py) owns the crawl browser entirely. See
.docs/architecture.md."""

import asyncio
import logging
import os
import secrets
import yaml
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from portal.paths import APP_DIR, LIVE_CONFIG_PATH
from shared.errors import format_validation_errors
from . import (
    admin,
    audit,
    auth,
    blacklist,
    campaigns,
    config,
    coordination,
    credentials,
    deps,
    domains,
    frontend,
    imports,
    jobs,
    leads,
    oauth,
    system,
    templates,
)
from .deps import ForbiddenPageException, RedirectException, get_current_user, verify_csrf
from ..db import Database

log = logging.getLogger(__name__)


def _ensure_jwt_secret(config_dict: dict, config_path: Path) -> None:
    """Generate + persist a random JWT secret on first run so it survives restarts.

    Containers (deploy/docker-compose.yml) supply JWT_SECRET via env instead —
    skip the file write there, since config.yaml isn't guaranteed writable/
    persistent inside the image.

    JWT_SECRET_PREV (optional) supports rotation without mass-logout: deps.
    get_current_user/auth.bootstrap try jwt_secret first, then jwt_secret_prev
    on failure, so sessions signed under the old secret stay valid through
    their remaining (short) access-token TTL after a rotation. See
    deploy/SECURITY.md for the rotate procedure."""
    if os.environ.get("JWT_SECRET_PREV"):
        config_dict.setdefault("auth", {})["jwt_secret_prev"] = os.environ["JWT_SECRET_PREV"]
    if os.environ.get("JWT_SECRET"):
        config_dict.setdefault("auth", {})["jwt_secret"] = os.environ["JWT_SECRET"]
        return
    if config_dict.get("auth", {}).get("jwt_secret"):
        return
    config_dict.setdefault("auth", {})["jwt_secret"] = secrets.token_urlsafe(48)
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("Generated and persisted a new auth.jwt_secret")


_REAP_INTERVAL_SECONDS = 60
_REAP_THRESHOLD_SECONDS = 150  # lenient vs. per_url_timeout (~100s) + jitter, per plan.md §10.6
_STUCK_SENDING_THRESHOLD_SECONDS = 600  # far above the ~30s SMTP timeout, per plan.md §19


async def _reap_loop():
    while True:
        await asyncio.sleep(_REAP_INTERVAL_SECONDS)
        try:
            reaped = deps._db.reap_stale_jobs(_REAP_THRESHOLD_SECONDS)
            if reaped:
                log.warning(
                    f"Reaped {len(reaped)} stale job(s) (no heartbeat for {_REAP_THRESHOLD_SECONDS}s+): {reaped}"
                )
        except Exception:
            log.error("Stale-job reaper sweep failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    recovered = deps._db.recover_stuck_sending(_STUCK_SENDING_THRESHOLD_SECONDS)
    if recovered:
        log.warning(
            f"Requeued {len(recovered)} email(s) stuck SENDING (no completion for "
            f"{_STUCK_SENDING_THRESHOLD_SECONDS}s+): {recovered}"
        )

    reap_task = asyncio.create_task(_reap_loop())
    yield
    reap_task.cancel()
    try:
        await reap_task
    except asyncio.CancelledError:
        pass


def create_app(config_dict: dict, db: Database) -> FastAPI:
    deps._db = db
    deps._config = config_dict
    deps._config_path = LIVE_CONFIG_PATH

    _ensure_jwt_secret(config_dict, deps._config_path)

    app = FastAPI(title="GovCrawler Portal", lifespan=lifespan)

    # The Caddy proxy (deploy/Caddyfile) serves frontend + API from one
    # origin, so CORS is defense-in-depth, not load-bearing — no admin_origin
    # configured means no cross-origin browser access at all (loopback/dev
    # tools like curl aren't subject to CORS regardless).
    admin_origin = config_dict.get("auth", {}).get("admin_origin")
    if admin_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[admin_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
        )

    @app.exception_handler(RedirectException)
    async def _redirect_handler(request: Request, exc: RedirectException):
        return RedirectResponse(url=exc.location, status_code=302)

    # A logged-in visitor without the needed permission gets a page they can
    # actually read, not a raw JSON 403 — see deps.require_page().
    @app.exception_handler(ForbiddenPageException)
    async def _forbidden_page_handler(request: Request, exc: ForbiddenPageException):
        template = frontend._templates.get_template("access-denied.html")
        html = template.render({"request": request, "active_page": "access-denied", "message": exc.message})
        return HTMLResponse(html, status_code=403)

    # Every error response is guaranteed a plain-string `detail` — the
    # frontend's shared apiFetch()/friendlyMessage() (frontend/shared/
    # static/js/http.js) never has to render FastAPI's default
    # `[{loc, msg, type}, ...]` 422 array or a raw traceback.
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        content = {"detail": format_validation_errors(exc), "code": "validation_error"}
        return JSONResponse(status_code=422, content=content)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        log.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
        content = {"detail": "Something went wrong on the server.", "code": "internal_error"}
        return JSONResponse(status_code=500, content=content)

    # Mount static files: /static is this tier's own (frontend/cloud/static),
    # /assets is the tier-agnostic shared tree (frontend/shared/static) — kept
    # as distinct prefixes, not nested, so there's no StaticFiles mount-order
    # ambiguity between the two.
    static_dir = APP_DIR / "frontend" / "cloud" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    shared_static_dir = APP_DIR / "frontend" / "shared" / "static"
    app.mount("/assets", StaticFiles(directory=str(shared_static_dir)), name="assets")

    # verify_csrf alongside get_current_user on every mutation-capable router —
    # it only enforces on non-GET requests with no Authorization header (i.e.
    # cookie-authenticated browser requests), so it's a no-op for the agent's
    # Bearer-token calls to coordination.
    app.include_router(auth.router)
    app.include_router(oauth.router)
    # admin.router's routes were previously mounted without verify_csrf — a
    # real (defense-in-depth, not primary-vector — SameSite=Strict already
    # covers it) gap, closed incidentally while adding the permission-override
    # route here (plan.md §19.1 Phase 9 Part 2, 2.0).
    app.include_router(admin.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(audit.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(frontend.router)
    app.include_router(domains.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(config.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(imports.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(jobs.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(coordination.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(leads.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(templates.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(blacklist.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(campaigns.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(credentials.router, dependencies=[Depends(get_current_user), Depends(verify_csrf)])
    app.include_router(system.router, dependencies=[Depends(verify_csrf)])

    return app
