"""
Frontend HTML page routes plus small UI-support endpoints.

Registers routes:
  GET    /                  → domains browser page
  GET    /leads             → leads page
  GET    /settings          → settings page
  GET    /test-campaign     → test campaign page
  GET    /campaigns         → campaigns page
  GET    /user-guide        → user guide page
  GET    /api/logs          → last 1000 lines of portal.log
  DELETE /api/visited-urls  → clear the recrawl-protection cache
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import Database
from .deps import get_db

router = APIRouter(tags=["frontend"])

_frontend_dir = Path(__file__).parent.parent / "frontend"
_templates = Jinja2Templates(directory=str(_frontend_dir))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = _templates.get_template("index.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request):
    template = _templates.get_template("leads.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    template = _templates.get_template("settings.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/test-campaign", response_class=HTMLResponse)
async def test_campaign_page(request: Request):
    template = _templates.get_template("test-campaign.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    template = _templates.get_template("campaigns.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/user-guide", response_class=HTMLResponse)
async def user_guide_page(request: Request):
    template = _templates.get_template("user-guide.html")
    return HTMLResponse(template.render({"request": request}))


@router.get("/api/logs")
async def get_logs():
    log_file = Path("portal/data/portal.log")
    if not log_file.exists():
        return {"logs": "Log file not found."}
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-1000:]
        return {"logs": "".join(lines)}
    except Exception as e:
        return {"logs": f"Failed to read logs: {e}"}


@router.delete("/api/visited-urls")
async def clear_visited_urls(db: Database = Depends(get_db)):
    db.clear_visited_urls()
    return {"message": "Visited URLs cleared."}
