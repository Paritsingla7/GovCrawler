from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..tables.crawl import Domain
from ..tables.leads import Lead
from ...services.lead_scoring import compute_lead_score


class LeadMixin:
    def get_lead_score_weights(self) -> dict:
        return self._lead_score_weights

    def save_lead(self, job_id: int, domain_id: int | None, email: str | None,
                  person_name: str | None, designation: str | None,
                  department: str | None, source_url: str, source_title: str | None,
                  context_snippet: str, entity_kind: str | None = None,
                  phone: str | None = None, channel_tag: str | None = None,
                  confidence_band: str | None = None,
                  field_provenance: str | None = None, depth: int = 0) -> bool:
        if not email:
            return False
        email = email.lower()
        with self._Session() as s:
            existing = s.query(Lead.id).filter(Lead.email == email).first()
            if existing:
                return False

            domain_state = None
            domain_org_type = None
            if domain_id:
                row = s.query(Domain.state, Domain.org_type).filter_by(id=domain_id).first()
                if row:
                    domain_state, domain_org_type = row.state, row.org_type
            lead_score = compute_lead_score({
                "email": email, "phone": phone, "person_name": person_name,
                "designation": designation,
            }, confidence_band=confidence_band, channel_tag=channel_tag,
                weights=self._lead_score_weights)
            try:
                s.add(Lead(
                    job_id=job_id, domain_id=domain_id, email=email,
                    person_name=person_name, designation=designation,
                    department=department, source_url=source_url,
                    source_title=source_title,
                    context_snippet=context_snippet,
                    domain_state=domain_state, domain_org_type=domain_org_type,
                    entity_kind=entity_kind, phone=phone, channel_tag=channel_tag,
                    confidence_band=confidence_band, field_provenance=field_provenance,
                    lead_score=lead_score, depth=depth,
                ))
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    @staticmethod
    def _apply_lead_filters(q, job_id=None, category=None, state=None,
                            search=None, complete_only=False, min_score=None):
        if job_id is not None:
            q = q.filter(Lead.job_id == job_id)
        if category:
            q = q.filter(Domain.category_code == category)
        if state:
            q = q.filter(Domain.state == state)
        if search:
            q = q.filter(
                or_(Lead.email.ilike(f"%{search}%"),
                    Lead.person_name.ilike(f"%{search}%"),
                    Lead.department.ilike(f"%{search}%"),
                    Lead.designation.ilike(f"%{search}%"))
            )
        if complete_only:
            q = q.filter(
                Lead.person_name.isnot(None), Lead.person_name != "",
                Lead.designation.isnot(None), Lead.designation != "",
                Lead.department.isnot(None), Lead.department != "",
            )
        if min_score is not None:
            q = q.filter(Lead.lead_score >= min_score)
        return q

    def get_leads(self, job_id: int | None = None, category: str = None,
                  state: str = None, search: str = None, page: int = 1,
                  limit: int = 100, complete_only: bool = False,
                  min_score: int | None = None) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            q = self._apply_lead_filters(q, job_id, category, state, search, complete_only, min_score)

            total = q.count()
            offset = (page - 1) * limit
            rows = q.order_by(Lead.captured_at.desc()).offset(offset).limit(limit).all()
            return (
                [{"id": l.id, "email": l.email, "person_name": l.person_name,
                  "designation": l.designation, "department": l.department,
                  "source_url": l.source_url, "source_title": l.source_title,
                  "context_snippet": l.context_snippet,
                  "domain_title": dt, "category_code": cc,
                  "domain_state": l.domain_state, "domain_org_type": l.domain_org_type,
                  "confidence_band": l.confidence_band,
                  "field_provenance": l.field_provenance,
                  "channel_tag": l.channel_tag,
                  "phone": l.phone,
                  "lead_score": l.lead_score or 0,
                  "depth": l.depth or 0,
                  "captured_at": l.captured_at.isoformat() if l.captured_at else None}
                 for l, dt, cc in rows],
                total,
            )

    def get_lead_ids(self, job_id: int | None = None, category: str = None,
                     state: str = None, search: str = None,
                     complete_only: bool = False, min_score: int | None = None) -> list[int]:
        with self._Session() as s:
            q = s.query(Lead.id).outerjoin(Domain, Lead.domain_id == Domain.id)
            q = self._apply_lead_filters(q, job_id, category, state, search, complete_only, min_score)
            return [r[0] for r in q.all()]

    def get_all_leads_for_export(self, job_id: int | None = None,
                                 category: str = None, state: str = None,
                                 search: str = None, lead_ids: list[int] = None,
                                 complete_only: bool = False,
                                 min_score: int | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code, Domain.category_title)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            if lead_ids:
                q = q.filter(Lead.id.in_(lead_ids))
            else:
                q = self._apply_lead_filters(q, job_id, category, state, search, complete_only, min_score)
            rows = q.order_by(Lead.domain_id, Lead.captured_at).all()
            return [
                {"email": l.email, "person_name": l.person_name or "",
                 "designation": l.designation or "", "department": l.department or "",
                 "domain_title": dt or "", "domain_state": l.domain_state or "",
                 "domain_org_type": l.domain_org_type or "",
                 "category_title": ct or cc or "",
                 "source_url": l.source_url or "",
                 "source_title": l.source_title or "",
                 "context_snippet": l.context_snippet or "",
                 "confidence_band": l.confidence_band or "",
                 "field_provenance": l.field_provenance or "",
                 "phone": l.phone or "",
                 "lead_score": l.lead_score or 0,
                 "depth": l.depth or 0,
                 "captured_at": l.captured_at.isoformat() if l.captured_at else ""}
                for l, dt, cc, ct in rows
            ]

    def get_lead_categories(self, job_id: int | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Domain.category_code, Domain.category_title,
                        func.count(Lead.id).label("count"))
                .join(Lead, Lead.domain_id == Domain.id)
            )
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            rows = (
                q.group_by(Domain.category_code, Domain.category_title)
                .order_by(func.count(Lead.id).desc())
                .all()
            )
            return [
                {"code": r.category_code,
                 "title": r.category_title or r.category_code,
                 "count": r.count}
                for r in rows
            ]

    def get_lead_states(self, job_id: int | None = None, category: str = None) -> list[str]:
        with self._Session() as s:
            q = s.query(Domain.state).join(Lead, Lead.domain_id == Domain.id).filter(Domain.state.isnot(None))
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            if category:
                q = q.filter(Domain.category_code == category)
            rows = q.distinct().order_by(Domain.state).all()
            return [r[0] for r in rows if r[0]]

    def bulk_upsert_manual_leads(self, job_id: int, rows: list[dict]) -> tuple[int, int, list[dict]]:
        """Insert/update CSV-uploaded leads. Updates a row only if the existing
        lead with that email is itself manual; leaves crawled leads untouched."""
        imported = 0
        updated = 0
        skipped: list[dict] = []
        with self._Session() as s:
            for row in rows:
                existing = s.query(Lead).filter(Lead.email == row["email"]).first()
                if existing:
                    if existing.channel_tag == "manual":
                        existing.person_name = row.get("name") or existing.person_name
                        existing.designation = row.get("designation") or existing.designation
                        existing.department = row.get("department") or existing.department
                        existing.phone = row.get("phone") or existing.phone
                        existing.lead_score = compute_lead_score({
                            "email": existing.email, "phone": existing.phone,
                            "person_name": existing.person_name, "designation": existing.designation,
                        }, confidence_band=existing.confidence_band, channel_tag=existing.channel_tag,
                            weights=self._lead_score_weights)
                        updated += 1
                    else:
                        skipped.append({
                            "row": row.get("row"), "email": row["email"],
                            "reason": "email already exists as a crawled lead",
                        })
                    continue
                lead_score = compute_lead_score({
                    "email": row["email"], "phone": row.get("phone"),
                    "person_name": row.get("name"), "designation": row.get("designation"),
                }, confidence_band=None, channel_tag="manual",
                    weights=self._lead_score_weights)
                s.add(Lead(
                    job_id=job_id, domain_id=None, email=row["email"],
                    person_name=row.get("name"), designation=row.get("designation"),
                    department=row.get("department"), source_url="manual-csv-upload",
                    source_title=None, context_snippet=None,
                    phone=row.get("phone"), channel_tag="manual",
                    lead_score=lead_score, depth=0,
                ))
                imported += 1
            s.commit()
        return imported, updated, skipped

    _LEAD_EDITABLE = frozenset({"person_name", "designation", "department", "domain_state"})

    def update_lead(self, lead_id: int, updates: dict) -> bool:
        safe = {
            k: (v.strip() if isinstance(v, str) and v.strip() else None)
            for k, v in updates.items()
            if k in self._LEAD_EDITABLE
        }
        if not safe:
            return False
        with self._Session() as s:
            lead = s.query(Lead).filter_by(id=lead_id).first()
            if not lead:
                return False
            for k, v in safe.items():
                setattr(lead, k, v)
            lead.lead_score = compute_lead_score({
                "email": lead.email, "phone": lead.phone,
                "person_name": lead.person_name, "designation": lead.designation,
            }, confidence_band=lead.confidence_band, channel_tag=lead.channel_tag,
                weights=self._lead_score_weights)
            s.commit()
            return True
