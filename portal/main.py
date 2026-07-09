"""
GovCrawler Portal — entry point.

Usage:
    python -m portal                          # start the server (default)
    python -m portal serve                    # same
    python -m portal import-json              # seed DB from gov_domains.json (zero API calls)
    python -m portal import-json path/to.json # seed from a specific file
    python -m portal import                   # refresh from live india.gov.in API
    python -m portal crawl <job_id>           # manually run a specific job (debug)
    python -m portal create-admin <email> [password]  # provision the first admin user
"""

import asyncio
import logging
import sys

import uvicorn

from cloud.api.server import create_app
from cloud.db import Database
from .config import load_config
from .paths import LOG_FILE_PATH, bootstrap

# First-run setup must precede the late imports below (they read env it sets).
bootstrap()

log_handlers = [logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")]

# Only attach the terminal output if the terminal actually exists
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=log_handlers,
)
log = logging.getLogger(__name__)

# httpx (and its transport, httpcore) log an INFO line for every single request —
# the crawler's HTTPX-first fetches and the launcher's activity polling would
# otherwise flood the log file with one line per page/poll.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_serve(config: dict):
    db = Database(config)
    app = create_app(config, db)
    host = config["api"]["host"]
    port = config["api"]["port"]
    # Windows browsers cannot route to 0.0.0.0. If your config uses 0.0.0.0
    # to allow local network traffic, we must open the browser at 127.0.0.1.
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{display_host}:{port}"

    log.info(f"Portal starting at {url}")
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_import_json(config: dict, json_path: str = "gov_domains.json"):
    from cloud.services.importer import import_from_json

    db = Database(config)
    log.info(f"Importing from {json_path} — zero API calls…")
    import_from_json(db, json_path, config)
    count = db.count_domains()
    log.info(f"JSON import finished. {count} domains in DB.")
    db.close()


def cmd_create_admin(config: dict, email: str, password: str | None = None):
    import getpass

    db = Database(config)
    if db.get_user_by_email(email):
        log.error(f"A user with email {email} already exists.")
        db.close()
        sys.exit(1)
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            log.error("Passwords do not match.")
            db.close()
            sys.exit(1)
    user_id = db.create_user(email=email, password=password, is_admin=True)
    log.info(f"Admin user created: {email} (id={user_id})")
    db.close()


def cmd_import(config: dict):
    from cloud.services.importer import import_all

    db = Database(config)
    log.info("Starting live API import…")
    import_all(db, config)
    log.info(f"API import finished. {db.count_domains()} domains in DB.")
    db.close()


async def cmd_crawl(config: dict, job_id: int):
    """Debug re-run of an existing job. No uvicorn server needs to already be
    running — coordination calls go in-process via httpx.ASGITransport
    against a throwaway app instance instead of a real network hop, since the
    engine only speaks CloudApiClient now (see plan.md §8)."""
    import httpx
    from playwright.async_api import async_playwright

    from agent.api import _local_visited_bootstrap
    from agent.cloud_client import CloudApiClient, resume_remote_job
    from agent.crawler.engine import CrawlerEngine
    from cloud.api.server import create_app
    from cloud.security.jwt import create_access_token
    from .paths import DATA_DIR

    db = Database(config)
    job = db.get_job(job_id, view_all=True)
    if not job:
        log.error(f"Job {job_id} not found.")
        db.close()
        return

    owner_id = job.get("owner_id")
    if owner_id is None:
        admin = next((u for u in db.list_users() if u["is_admin"]), None)
        if not admin:
            log.error("No user to attribute this debug run to (job has no owner_id and no admin exists).")
            db.close()
            return
        owner_id = admin["id"]
    token_version = db.get_user_by_id(owner_id)["token_version"]
    secret = config["auth"]["jwt_secret"]
    ttl = config["auth"].get("access_ttl_minutes", 15)

    async def token_provider():
        # Mints a fresh token per call via direct DB/JWT-secret access — fine
        # here since portal.main is the debug-CLI entrypoint, not the agent
        # runtime (which no longer has either, plan.md §19.1 Phase 9). Never
        # actually expires from the crawl's perspective, so "refresh" is the
        # same function: calling it again just re-mints.
        return create_access_token(owner_id, token_version, secret, ttl)

    app = create_app(config, db)
    transport = httpx.ASGITransport(app=app)

    resumed = await resume_remote_job("http://local", token_provider, token_provider, job_id, transport=transport)
    seeds = [(s[0], s[1]) for s in resumed["seeds"]]
    engine_config = resumed["policy"]
    recrawl_days = engine_config.get("crawler", {}).get("recrawl_days", 30)
    visited_bootstrap = _local_visited_bootstrap(seeds, recrawl_days)

    log.info(f"Running job {job_id} with {len(seeds)} seeds…")

    outbox_path = DATA_DIR / f"outbox_job_{job_id}.db"
    cloud = CloudApiClient(
        "http://local", token_provider, job_id, outbox_path, transport=transport, refresh=token_provider
    )
    cloud.start()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            engine = CrawlerEngine(config=engine_config, cloud=cloud, job_id=job_id, browser=browser)
            await engine.run(seeds, visited_bootstrap=visited_bootstrap)
            await cloud.finish_job(status="done")
        except Exception as e:
            log.error(f"Job {job_id} failed: {e}", exc_info=True)
            await cloud.finish_job(status="failed", error=str(e))
        finally:
            await browser.close()
            await cloud.aclose()

    updated = db.get_job(job_id, view_all=True)
    log.info(f"Job {job_id} finished. Leads: {updated['leads_found']}")
    db.close()


def main():
    config = load_config()

    args = sys.argv[1:]
    cmd = args[0] if args else "serve"

    if cmd in ("serve", ""):
        cmd_serve(config)
    elif cmd == "import-json":
        json_path = args[1] if len(args) > 1 else "gov_domains.json"
        cmd_import_json(config, json_path)
    elif cmd == "import":
        cmd_import(config)
    elif cmd == "crawl":
        if len(args) < 2:
            print("Usage: python -m portal crawl <job_id>")
            sys.exit(1)
        asyncio.run(cmd_crawl(config, int(args[1])))
    elif cmd == "create-admin":
        if len(args) < 2:
            print("Usage: python -m portal create-admin <email> [password]")
            sys.exit(1)
        password = args[2] if len(args) > 2 else None
        cmd_create_admin(config, args[1], password)
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: serve | import-json [path] | import | crawl <job_id> | create-admin <email> [password]")
        sys.exit(1)


if __name__ == "__main__":
    main()
