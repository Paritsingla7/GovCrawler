"""
MishaCrawler Portal v2 — entry point.

Usage:
    python -m portal                   # starts the portal server (default)
    python -m portal serve             # same as above
    python -m portal import            # one-shot: import from india.gov.in API then exit
    python -m portal crawl <job_id>    # manually trigger a specific job (for debugging)

The server exposes http://<host>:<port> where the frontend + API live.
All configuration is in portal/config.yaml — nothing is hardcoded here.
"""

import asyncio
import logging
import sys
from pathlib import Path

import yaml
import uvicorn

from .db.database import Database
from .api.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("portal/data/portal.log"),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        log.error(f"Config not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def cmd_serve(config: dict):
    """Start the FastAPI + Uvicorn server."""
    db = Database(config)
    app = create_app(config, db)

    host = config["api"]["host"]
    port = config["api"]["port"]
    log.info(f"Portal starting at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_import(config: dict):
    """Blocking one-shot import from india.gov.in API."""
    from .scraper.india_gov import import_all
    db = Database(config)
    log.info("Starting one-shot import…")
    import_all(db, config)
    log.info(f"Import finished. {db.count_domains()} domains in DB.")
    db.close()


async def cmd_crawl(config: dict, job_id: int):
    """Manually run a specific crawl job (debugging use)."""
    import json
    from playwright.async_api import async_playwright
    from .crawler.engine import CrawlerEngine

    db = Database(config)
    job = db.get_job(job_id)
    if not job:
        log.error(f"Job {job_id} not found in database.")
        db.close()
        return

    domain_ids = json.loads(job["domain_ids"])
    domains = db.get_domains_by_ids(domain_ids)
    seeds = [(d["contact_url"] or d["main_url"], d["id"]) for d in domains if (d["contact_url"] or d["main_url"])]

    log.info(f"Running job {job_id} with {len(seeds)} seeds…")
    db.start_job(job_id)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            engine = CrawlerEngine(config=config, db=db, job_id=job_id, browser=browser)
            await engine.run(seeds)
            db.finish_job(job_id, status="done")
        except Exception as e:
            log.error(f"Job {job_id} failed: {e}", exc_info=True)
            db.finish_job(job_id, status="failed", error=str(e))
        finally:
            await browser.close()

    updated = db.get_job(job_id)
    log.info(f"Job {job_id} finished. Leads: {updated['leads_found']}")
    db.close()


def main():
    config = load_config()
    # Ensure log/data dir exists
    Path("portal/data").mkdir(parents=True, exist_ok=True)

    args = sys.argv[1:]
    cmd = args[0] if args else "serve"

    if cmd in ("serve", ""):
        cmd_serve(config)
    elif cmd == "import":
        cmd_import(config)
    elif cmd == "crawl":
        if len(args) < 2:
            print("Usage: python -m portal crawl <job_id>")
            sys.exit(1)
        asyncio.run(cmd_crawl(config, int(args[1])))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: serve | import | crawl <job_id>")
        sys.exit(1)


if __name__ == "__main__":
    main()
