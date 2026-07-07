"""
Domain import endpoints (JSON file upload + live india.gov.in API).

Registers routes:
  POST /api/import/json     → import from uploaded JSON file (zero API calls)
  POST /api/import          → import from live india.gov.in API
  GET  /api/import/status   → import progress
"""

import asyncio
import logging
import tempfile
from fastapi import APIRouter, Depends, File, UploadFile
from pathlib import Path

from .deps import CurrentUser, get_config as get_app_config, get_current_user, get_db, require
from ..db import Database
from ..scraper.importer import import_all, import_from_json, import_status

log = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

# Single-flight guard for imports. An in-process asyncio.Lock is sufficient
# here since this app still runs as a single Uvicorn process (multi-worker
# gunicorn is a later phase, blocked on the in-memory task-registry problem —
# see plan.md §12). Checking .locked() then awaiting .acquire() on an
# unlocked lock has no suspension point in between (the fast path in
# asyncio.Lock.acquire() completes synchronously), so this closes the
# check-then-act race the plain import_status["running"] flag had.
_import_lock = asyncio.Lock()


async def _run_json_import(db: Database, config: dict, json_path: str, cleanup: bool = False):
    log.info(f"Background JSON import started from {json_path}")
    try:
        await asyncio.to_thread(import_from_json, db, json_path, config)
    finally:
        if cleanup:
            Path(json_path).unlink(missing_ok=True)
        _import_lock.release()
    log.info("Background JSON import finished")


async def _run_import(db: Database, config: dict):
    log.info("Background API import started")
    try:
        await asyncio.to_thread(import_all, db, config)
    finally:
        _import_lock.release()
    log.info("Background API import finished")


@router.post("/api/import/json")
async def trigger_json_import(
        file: UploadFile = File(...),
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
        user: CurrentUser = Depends(require("domains.import")),
):
    """Import domains from an uploaded JSON file — zero API calls."""
    if _import_lock.locked():
        return {"message": "Import already running", "status": import_status}
    await _import_lock.acquire()
    content = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.write(content)
    tmp.close()
    asyncio.create_task(_run_json_import(db, config, tmp.name, cleanup=True))
    return {"message": f"JSON import started from {file.filename}"}


@router.post("/api/import")
async def trigger_import(db: Database = Depends(get_db), config: dict = Depends(get_app_config),
                         user: CurrentUser = Depends(require("domains.import"))):
    """Import from live india.gov.in API — use only to refresh data."""
    if _import_lock.locked():
        return {"message": "Import already running", "status": import_status}
    await _import_lock.acquire()
    asyncio.create_task(_run_import(db, config))
    return {"message": "API import started"}


@router.get("/api/import/status")
async def get_import_status(user: CurrentUser = Depends(get_current_user)):
    return import_status
