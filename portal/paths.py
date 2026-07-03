"""
Application path resolution and first-run bootstrap.

Handles both dev mode (running from source) and PyInstaller frozen mode
(running as GovCrawler.exe).
"""
import os
import shutil
import sys
from pathlib import Path


def get_app_dir() -> Path:
    """The root directory (Writeable)."""
    if getattr(sys, 'frozen', False):
        # Compiled: Returns the folder where the .exe physically lives
        return Path(sys.executable).parent
    # Native: Steps up from /project_root/portal/paths.py -> /project_root
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
ICON_PATH = BUNDLE_DIR / "assets" / "favicon.ico"


def bootstrap() -> None:
    """First-run setup: create data dirs, copy default config if needed, and
    point Playwright at the bundled/local browser directory."""
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
