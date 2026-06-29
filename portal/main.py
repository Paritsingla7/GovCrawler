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

# ==========================================
# 1. PATH MANAGER
# ==========================================
def get_app_dir() -> Path:
    """The root directory (Writeable)."""
    if getattr(sys, 'frozen', False):
        # Compiled: Returns the folder where the .exe physically lives
        return Path(sys.executable).parent
    # Native: Steps up from /project_root/portal/main.py -> /project_root
    return Path(__file__).resolve().parent.parent

def get_bundle_dir() -> Path:
    """The temporary PyInstaller extraction folder (Read-Only)."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    # Native: Steps up to project root
    return Path(__file__).resolve().parent.parent

APP_DIR = get_app_dir()
BUNDLE_DIR = get_bundle_dir()

# --- WRITEABLE PATHS (Next to the .exe) ---
PORTAL_LIVE_DIR = APP_DIR / "portal"
DATA_DIR = PORTAL_LIVE_DIR / "data"

LOG_FILE_PATH = DATA_DIR / "portal.log"
LIVE_CONFIG_PATH = PORTAL_LIVE_DIR / "config.yaml"

# --- READ-ONLY PATHS (Inside the bundle) ---
BROWSER_PATH = APP_DIR / "playwright_browsers"
DEFAULT_CONFIG_PATH = BUNDLE_DIR / "portal" / "default_config.yaml"

# ==========================================
# 2. FIRST-RUN SETUP & ENVIRONMENT
# ==========================================
# Only execute the copy logic if running as an executable
if getattr(sys, 'frozen', False) and not LIVE_CONFIG_PATH.exists():
    PORTAL_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DEFAULT_CONFIG_PATH.exists():
        shutil.copy(DEFAULT_CONFIG_PATH, LIVE_CONFIG_PATH)
else:
    # Safe to create in development mode too
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Force Playwright to use the bundled browser path BEFORE importing Playwright
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_PATH)

# ==========================================
# 3. LATE IMPORTS (Safe now that env is set)
# ==========================================
import yaml
import uvicorn
from .db.models import Database
from .api.server import create_app

# ==========================================
# 4. LOGGING SETUP (Using Absolute Path)
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
    db  = Database(config)
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

    db  = Database(config)
    job = db.get_job(job_id)
    if not job:
        log.error(f"Job {job_id} not found.")
        db.close()
        return

    domain_ids = json.loads(job.get("domain_ids") or "[]")
    domains    = db.get_domains_by_ids(domain_ids)
    seeds      = [(d["contact_url"] or d["main_url"], d["id"])
                  for d in domains if (d["contact_url"] or d["main_url"])]

    log.info(f"Running job {job_id} with {len(seeds)} seeds…")
    db.start_job(job_id)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            engine = CrawlerEngine(config=config, db=db,
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
    cmd  = args[0] if args else "serve"

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
    