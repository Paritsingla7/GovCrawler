"""load_config() — split out of portal/main.py so cloud-only entrypoints
(cloud/dispatch_service.py) can read config without transitively importing
portal.main's debug CLI, which imports agent.* (cmd_crawl) — that indirect
edge is exactly what the import-linter's "cloud must not import agent"
contract exists to catch (plan.md §19.1 Phase 9 Part 2, 2.7).

load_agent_config() is the agent-tier counterpart, reading a separate file
(agent_config.yaml, not config.yaml) — the two tiers no longer share a
config file at all, only this loader module."""

import logging
import os
import shutil
import yaml
from pathlib import Path

from .paths import AGENT_DEFAULT_CONFIG_PATH, AGENT_LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH, LIVE_CONFIG_PATH, bootstrap

log = logging.getLogger(__name__)


def _read_yaml(live_path: Path, default_path: Path) -> dict:
    target_config = live_path if live_path.exists() else (Path(__file__).parent / live_path.name)
    if not target_config.exists():
        log.error(f"Config not found at: {target_config}")
        os.makedirs(target_config.parent, exist_ok=True)
        shutil.copy(default_path, target_config)
    with open(target_config) as f:
        return yaml.safe_load(f)


def load_config() -> dict:
    """The cloud's own config (database, auth, dispatch, scraper, and the
    full crawl-policy content that seeds app_settings on first boot)."""
    bootstrap(LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    config = _read_yaml(LIVE_CONFIG_PATH, DEFAULT_CONFIG_PATH)

    # Container deployments (deploy/docker-compose.yml) point at Postgres via
    # env var rather than baking a second config.yaml into the image.
    # DATABASE_URL_APP (the least-privilege govcrawler_app role, see Alembic
    # 0020) takes precedence for runtime traffic when set; the `migrate`
    # service never sets it, so it always runs migrations with DATABASE_URL's
    # (superuser-ish) DDL rights. Local/dev/desktop installs without the
    # split role keep working on plain DATABASE_URL or the sqlite default.
    if os.environ.get("DATABASE_URL_APP"):
        config["database"]["uri"] = os.environ["DATABASE_URL_APP"]
    elif os.environ.get("DATABASE_URL"):
        config["database"]["uri"] = os.environ["DATABASE_URL"]
    if os.environ.get("DISPATCH_MODE"):
        config.setdefault("dispatch", {})["mode"] = os.environ["DISPATCH_MODE"]
    if os.environ.get("ADMIN_ORIGIN"):
        config.setdefault("auth", {})["admin_origin"] = os.environ["ADMIN_ORIGIN"]
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
