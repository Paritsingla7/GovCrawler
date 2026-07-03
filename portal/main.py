"""
GovCrawler Portal — entry point.

Usage:
    python -m portal                          # start the server (default)
    python -m portal serve                    # same
    python -m portal import-json              # seed DB from gov_domains.json (zero API calls)
    python -m portal import-json path/to.json # seed from a specific file
    python -m portal import                   # refresh from live india.gov.in API
    python -m portal crawl <job_id>           # manually run a specific job (debug)
"""
import os
import sys
import shutil
import asyncio
import logging
from pathlib import Path

from .paths import LOG_FILE_PATH, LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH, bootstrap

# ==========================================
# 1. FIRST-RUN SETUP & ENVIRONMENT
# ==========================================
bootstrap()

# ==========================================
# 2. LATE IMPORTS (Safe now that env is set)
# ==========================================
import yaml
import uvicorn
from .db import Database
from .api.server import create_app

# ==========================================
# 3. LOGGING SETUP (Using Absolute Path)
# ==========================================
# Safely configure log handlers
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


def load_config() -> dict:
    # Always read from the LIVE config next to the .exe
    target_config = LIVE_CONFIG_PATH if LIVE_CONFIG_PATH.exists() else (Path(__file__).parent / "config.yaml")

    if not target_config.exists():
        log.error(f"Config not found at: {target_config}")
        os.makedirs(target_config.parent, exist_ok=True)
        shutil.copy(DEFAULT_CONFIG_PATH, target_config)
    with open(target_config) as f:
        return yaml.safe_load(f)


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
    from .scraper.importer import import_from_json
    db = Database(config)
    log.info(f"Importing from {json_path} — zero API calls…")
    import_from_json(db, json_path, config)
    count = db.count_domains()
    log.info(f"JSON import finished. {count} domains in DB.")
    db.close()


def cmd_import(config: dict):
    from .scraper.importer import import_all
    db = Database(config)
    log.info("Starting live API import…")
    import_all(db, config)
    log.info(f"API import finished. {db.count_domains()} domains in DB.")
    db.close()


async def cmd_crawl(config: dict, job_id: int):
    import json
    from playwright.async_api import async_playwright
    from .crawler.engine import CrawlerEngine

    db = Database(config)
    job = db.get_job(job_id)
    if not job:
        log.error(f"Job {job_id} not found.")
        db.close()
        return

    engine_config = config
    if job.get("source_type") == "custom_urls":
        urls = [u["main_url"] for u in db.get_job_custom_urls(job_id)]
        seeds = [(url, None) for url in urls]
        engine_config = {**config, "crawler": {**config["crawler"], "target_suffixes": []}}
    else:
        domain_ids = json.loads(job.get("domain_ids") or "[]")
        domains = db.get_domains_by_ids(domain_ids)
        seeds = [(d["contact_url"] or d["main_url"], d["id"])
                 for d in domains if (d["contact_url"] or d["main_url"])]

    log.info(f"Running job {job_id} with {len(seeds)} seeds…")
    db.start_job(job_id)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            engine = CrawlerEngine(config=engine_config, db=db,
                                   job_id=job_id, browser=browser)
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
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: serve | import-json [path] | import | crawl <job_id>")
        sys.exit(1)


if __name__ == "__main__":
    main()
