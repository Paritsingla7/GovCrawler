"""load_config() — split out of portal/main.py so cloud-only entrypoints
(cloud/dispatch_service.py) can read config without transitively importing
portal.main's debug CLI, which imports agent.* (cmd_crawl) — that indirect
edge is exactly what the import-linter's "cloud must not import agent"
contract exists to catch (plan.md §19.1 Phase 9 Part 2, 2.7).

load_agent_config() is the agent-tier counterpart, reading a separate file
(agent_config.yaml, not config.yaml) — the two tiers no longer share a
config file at all, only this loader module.

Config layering (cloud):
  1. _CLOUD_DEFAULTS — complete hardcoded baseline; Docker needs nothing else.
  2. config.yaml deep-merge — optional; used in dev/desktop installs.
  3. Env-var overrides — DATABASE_URL*, JWT_SECRET, DISPATCH_MODE, etc.

This means the Dockerfile no longer needs to COPY a config.docker.yaml into
the image, and deploy/config.docker.yaml has been removed."""

import copy
import logging
import os
import shutil
import yaml
from pathlib import Path

from .paths import AGENT_DEFAULT_CONFIG_PATH, AGENT_LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH, LIVE_CONFIG_PATH, bootstrap

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Complete baseline — every key the cloud tier ever reads.  Docker containers
# rely entirely on this + env-var overrides; no YAML file is baked in.
# dev/desktop installs layer portal/config.yaml on top via deep_merge().
# ---------------------------------------------------------------------------
_CLOUD_DEFAULTS: dict = {
    "database": {
        "uri": "sqlite:///portal/data/govcrawler.db",
    },
    "api": {
        "host": "127.0.0.1",
        "port": 8001,
    },
    "scraper": {
        "category_filter": "",
        "org_type_filter": "",
    },
    "crawler": {
        "workers": 10,
        "max_depth": 4,
        "recrawl_days": 30,
        "httpx_first": True,
        "playwright_fallback": False,
        "httpx_timeout": {"connect": 10, "read": 30},
        "playwright_timeout": 45,
        "js_settle_time": 3.0,
        "per_url_timeout": 100,
        "request_delay": 1.5,
        "max_links_per_page": {0: 100, 1: 50, 2: 40, "default": 20},
        "target_suffixes": [".gov.in", ".nic.in"],
        "max_custom_urls": 50,
        "priority_keywords": [
            "contact",
            "officer",
            "directory",
            "whos-who",
            "who-is-who",
            "staff",
            "personnel",
            "secretariat",
            "about-us",
            "division",
            "minister",
            "committee",
            "administration",
            "team",
            "telephone",
            "tele-directory",
            "phone-directory",
            "email",
        ],
        "skip_extensions": [
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".ico",
            ".mp4",
            ".mp3",
            ".avi",
            ".mov",
        ],
        "js_indicators": [
            '<div id="__next"',
            '<div id="root"',
            "Please enable JavaScript",
            "You need to enable JavaScript",
            "This page requires JavaScript",
        ],
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "pagination": {
            "enabled": True,
            "max_pagination_pages": 50,
            "max_chain_children": 100,
            "text_signals": ["next", "\u00bb", "\u203a", "more", "last"],
            "param_signals": ["page", "pageno", "start", "offset", "p"],
        },
    },
    "extraction": {
        "email": {
            "enabled": True,
            "regex": r"[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}",
            "valid_suffixes": [".gov.in", ".nic.in", ".res.in", ".ac.in", ".com"],
            "obfuscation": [
                [r"\s*\[at\]\s*", "@"],
                [r"\s*\(at\)\s*", "@"],
                [r"\s*\[dot\]\s*", "."],
                [r"\s*\(dot\)\s*", "."],
                [r"\s*\[hyphen\]\s*", "-"],
                [r"\s*\(hyphen\)\s*", "-"],
            ],
            "context_chars": 200,
        },
        "max_input_chars": 200000,
        "role_local_parts": [
            "webmaster",
            "info",
            "admin",
            "contact",
            "support",
            "helpdesk",
            "grievance",
        ],
        "confidence": {
            "high_rungs": ["mailto_tel", "microdata"],
        },
        "person": {
            "enabled": True,
            "title_prefixes": [
                "Shri",
                "Smt",
                "Dr",
                "Mr",
                "Mrs",
                "Ms",
                "Prof",
                "Sh",
                "Shrimati",
                "Km",
            ],
            "designation_keywords": [
                "Secretary",
                "Director",
                "Commissioner",
                "Collector",
                "Superintendent",
                "Inspector",
                "Officer",
                "Manager",
                "Chairman",
                "President",
                "Minister",
                "Deputy",
                "Additional",
                "Principal",
                "Chief",
                "Joint",
                "Under Secretary",
                "IAS",
                "IPS",
                "IFS",
                "IRS",
                "Jt",
                "Jr",
                "Junior",
            ],
            "proximity_chars": 300,
        },
    },
    "lead_score": {
        "weights": {
            "email_high": 20,
            "email_low": 10,
            "person_name": 40,
            "designation": 30,
            "phone": 10,
        },
    },
    "auth": {
        "jwt_secret": "",
        "access_ttl_minutes": 15,
        "refresh_ttl_days": 14,
        # False in dev (HTTP localhost); Docker sets cookie_secure via
        # COOKIE_SECURE=true env var — cookies are only sent over HTTPS in prod.
        "cookie_secure": False,
        "lockout_threshold": 5,
        "lockout_minutes": 15,
        "login_rate_limit_attempts": 20,
        "login_rate_limit_window_minutes": 15,
    },
    "dispatch": {
        # Docker overrides this to "external" via DISPATCH_MODE env var.
        "mode": "embedded",
    },
    "oauth": {
        "redirect_base_url": "",
        "microsoft": {"client_id": ""},
        "google": {"client_id": "", "client_secret": ""},
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into a copy of *base*.  Scalar values in
    *overlay* win; nested dicts are merged depth-first so callers can override
    individual leaf keys without stomping sibling keys."""
    result = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _try_read_yaml(path: Path) -> dict | None:
    """Return the parsed YAML at *path*, or None if the file doesn't exist."""
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _read_yaml(live_path: Path, default_path: Path) -> dict:
    """Agent-config loader: always requires a real file (falls back to copying
    *default_path* when *live_path* is missing, just as before)."""
    target_config = live_path if live_path.exists() else (Path(__file__).parent / live_path.name)
    if not target_config.exists():
        log.error(f"Config not found at: {target_config}")
        os.makedirs(target_config.parent, exist_ok=True)
        shutil.copy(default_path, target_config)
    with open(target_config) as f:
        return yaml.safe_load(f)


def load_config() -> dict:
    """The cloud's own config (database, auth, dispatch, scraper, and the
    full crawl-policy content that seeds app_settings on first boot).

    Config layering:
      1. _CLOUD_DEFAULTS — self-sufficient baseline; no file needed in Docker.
      2. portal/config.yaml deep-merge — optional; present on dev/desktop.
      3. Env-var overrides — DATABASE_URL*, JWT_SECRET, DISPATCH_MODE, …
    """
    bootstrap(LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH)

    # Start from defaults, layer any local config.yaml on top.
    config = copy.deepcopy(_CLOUD_DEFAULTS)
    yaml_overrides = _try_read_yaml(LIVE_CONFIG_PATH)
    if yaml_overrides:
        config = _deep_merge(config, yaml_overrides)

    # ── Env-var overrides ───────────────────────────────────────────────────
    # Container deployments (deploy/docker-compose.yml) point at Postgres via
    # env var rather than baking a config.yaml into the image.
    # DATABASE_URL_APP (the least-privilege govcrawler_app role, see Alembic
    # 0020) takes precedence for runtime traffic when set; the `migrate`
    # service never sets it, so it always runs migrations with DATABASE_URL's
    # (superuser-ish) DDL rights.
    if os.environ.get("DATABASE_URL_APP"):
        config["database"]["uri"] = os.environ["DATABASE_URL_APP"]
    elif os.environ.get("DATABASE_URL"):
        config["database"]["uri"] = os.environ["DATABASE_URL"]

    if os.environ.get("API_HOST"):
        config["api"]["host"] = os.environ["API_HOST"]
    if os.environ.get("API_PORT"):
        config["api"]["port"] = int(os.environ["API_PORT"])

    if os.environ.get("DISPATCH_MODE"):
        config.setdefault("dispatch", {})["mode"] = os.environ["DISPATCH_MODE"]
    if os.environ.get("ADMIN_ORIGIN"):
        config.setdefault("auth", {})["admin_origin"] = os.environ["ADMIN_ORIGIN"]

    # cookie_secure: Docker sets COOKIE_SECURE=true; dev/desktop leaves it unset.
    if os.environ.get("COOKIE_SECURE"):
        config.setdefault("auth", {})["cookie_secure"] = os.environ["COOKIE_SECURE"].lower() == "true"

    if os.environ.get("OAUTH_REDIRECT_BASE_URL"):
        config.setdefault("oauth", {})["redirect_base_url"] = os.environ["OAUTH_REDIRECT_BASE_URL"]
    if os.environ.get("OAUTH_MS_CLIENT_ID"):
        config.setdefault("oauth", {}).setdefault("microsoft", {})["client_id"] = os.environ["OAUTH_MS_CLIENT_ID"]
    if os.environ.get("OAUTH_GOOGLE_CLIENT_ID"):
        config.setdefault("oauth", {}).setdefault("google", {})["client_id"] = os.environ["OAUTH_GOOGLE_CLIENT_ID"]
    if os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET"):
        config.setdefault("oauth", {}).setdefault("google", {})["client_secret"] = os.environ[
            "OAUTH_GOOGLE_CLIENT_SECRET"
        ]
    return config


def load_agent_config() -> dict:
    """The agent's own config — just api.host/port for its local BFF bind.
    No env-var overrides: those are all cloud-deployment (Docker) concerns
    that don't apply to an operator's desktop machine."""
    bootstrap(AGENT_LIVE_CONFIG_PATH, AGENT_DEFAULT_CONFIG_PATH)
    return _read_yaml(AGENT_LIVE_CONFIG_PATH, AGENT_DEFAULT_CONFIG_PATH)
