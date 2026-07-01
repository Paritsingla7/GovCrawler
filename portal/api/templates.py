"""
Email Template CRUD endpoints with mandatory Jinja2 validation.

Registers routes:
  GET    /api/templates          → list all templates
  GET    /api/templates/{id}     → single template
  POST   /api/templates          → create (validates Jinja2 syntax)
  PUT    /api/templates/{id}     → update (validates Jinja2 syntax)
  DELETE /api/templates/{id}     → delete template
"""

from fastapi import FastAPI, HTTPException
from jinja2 import Environment, TemplateSyntaxError
from pydantic import BaseModel

from ..db.models import Database


# ── Jinja2 validation ─────────────────────────────────────────────────────────

def validate_jinja2(raw_body: str) -> str | None:
    """Returns None if valid, error message string if invalid."""
    try:
        Environment().parse(raw_body)
        return None
    except TemplateSyntaxError as e:
        return f"Line {e.lineno}: {e.message}"


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    subject: str
    raw_body: str


class TemplateUpdate(BaseModel):
    name: str | None = None
    subject: str | None = None
    raw_body: str | None = None


# ── Route registration ────────────────────────────────────────────────────────

def register_template_routes(app: FastAPI, db: Database):
    @app.get("/api/templates")
    async def list_templates():
        return db.list_templates()

    @app.get("/api/templates/{template_id}")
    async def get_template(template_id: int):
        t = db.get_template(template_id)
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
        return t

    @app.post("/api/templates", status_code=201)
    async def create_template(req: TemplateCreate):
        # Validate Jinja2 syntax for both subject and body
        for field_name, field_val in [("subject", req.subject), ("raw_body", req.raw_body)]:
            err = validate_jinja2(field_val)
            if err:
                raise HTTPException(
                    status_code=400,
                    detail=f"Jinja2 syntax error in {field_name}: {err}",
                )

        tid = db.create_template(name=req.name, subject=req.subject,
                                 raw_body=req.raw_body)
        return {"id": tid, "message": "Template created"}

    @app.put("/api/templates/{template_id}")
    async def update_template(template_id: int, req: TemplateUpdate):
        existing = db.get_template(template_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Template not found")

        # Validate any Jinja2 fields being updated
        if req.subject is not None:
            err = validate_jinja2(req.subject)
            if err:
                raise HTTPException(
                    status_code=400,
                    detail=f"Jinja2 syntax error in subject: {err}",
                )
        if req.raw_body is not None:
            err = validate_jinja2(req.raw_body)
            if err:
                raise HTTPException(
                    status_code=400,
                    detail=f"Jinja2 syntax error in raw_body: {err}",
                )

        updates = req.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        db.update_template(template_id, **updates)
        return {"message": "Template updated"}

    @app.delete("/api/templates/{template_id}")
    async def delete_template(template_id: int):
        if not db.delete_template(template_id):
            raise HTTPException(status_code=404, detail="Template not found")
        return {"message": "Template deleted"}
