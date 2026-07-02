"""
Shared application state and FastAPI dependency providers.

State is set once in server.create_app()/lifespan() at startup. Route
handlers pull it via Depends(...) instead of capturing it through closures —
this lets each route module be imported and tested independently of app
construction.
"""
import asyncio
from pathlib import Path

from ..db import Database

_db: Database | None = None
_config: dict | None = None
_config_path: Path | None = None
_browser = None
_playwright_instance = None
_active_tasks: dict[int, asyncio.Task] = {}


def get_db() -> Database:
    return _db


def get_config() -> dict:
    return _config


def get_config_path() -> Path:
    return _config_path


def get_browser():
    return _browser


def get_active_tasks() -> dict[int, asyncio.Task]:
    return _active_tasks
