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
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from ..db import Database
from ..scraper.importer import import_all, import_from_json, import_status
from .deps import get_config as get_app_config, get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["import"])


async def _run_json_import(db: Database, config: dict, json_path: str, cleanup: bool = False):
    log.info(f"Background JSON import started from {json_path}")
    try:
        await asyncio.to_thread(import_from_json, db, json_path, config)
    finally:
        if cleanup:
            Path(json_path).unlink(missing_ok=True)
    log.info("Background JSON import finished")


async def _run_import(db: Database, config: dict):
    log.info("Background API import started")
    await asyncio.to_thread(import_all, db, config)
    log.info("Background API import finished")


@router.post("/api/import/json")
async def trigger_json_import(
        file: UploadFile = File(...),
        db: Database = Depends(get_db),
        config: dict = Depends(get_app_config),
):
    """Import domains from an uploaded JSON file — zero API calls."""
    if import_status.get("running"):
        return {"message": "Import already running", "status": import_status}
    content = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.write(content)
    tmp.close()
    asyncio.create_task(_run_json_import(db, config, tmp.name, cleanup=True))
    return {"message": f"JSON import started from {file.filename}"}


@router.post("/api/import")
async def trigger_import(db: Database = Depends(get_db), config: dict = Depends(get_app_config)):
    """Import from live india.gov.in API — use only to refresh data."""
    if import_status.get("running"):
        return {"message": "Import already running", "status": import_status}
    asyncio.create_task(_run_import(db, config))
    return {"message": "API import started"}


@router.get("/api/import/status")
async def get_import_status():
    return import_status
