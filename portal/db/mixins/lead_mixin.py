from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError

from ..tables.crawl import CrawlSnapshot
from ..tables.leads import Lead
from ...services.lead_scoring import compute_lead_score


class LeadMixin:
    def get_lead_score_weights(self) -> dict:
        return self._lead_score_weights

    def save_lead(self, job_id: int, snapshot_id: int | None, email: str | None,
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

            # Freeze state/org_type and recover the soft catalog link from the
            # per-crawl snapshot (immune to later domains-catalog rebuilds).
            domain_id = None
            domain_state = None
            domain_org_type = None
            if snapshot_id:
                snap = (
                    s.query(CrawlSnapshot.source_domain_id, CrawlSnapshot.state,
                            CrawlSnapshot.org_type)
                    .filter_by(id=snapshot_id).first()
                )
                if snap:
                    domain_id = snap.source_domain_id
                    domain_state, domain_org_type = snap.state, snap.org_type
            lead_score = compute_lead_score({
                "email": email, "phone": phone, "person_name": person_name,
                "designation": designation,
            }, confidence_band=confidence_band, channel_tag=channel_tag,
                weights=self._lead_score_weights)
            try:
                s.add(Lead(
                    job_id=job_id, domain_id=domain_id, snapshot_id=snapshot_id,
                    email=email,
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
    def _apply_lead_filters(q, job_ids=None, categories=None, states=None,
                            search=None, complete_only=False, min_score=None,
                            org_types=None, entry_type="both", require_name=False,
                            require_designation=False, require_phone=False):
        is_manual = Lead.channel_tag == "manual"
        if job_ids:
            q = q.filter(Lead.job_id.in_(job_ids))
        if entry_type == "manual":
            q = q.filter(Lead.channel_tag == "manual")
        elif entry_type == "extracted":
            q = q.filter(or_(Lead.channel_tag.is_(None), Lead.channel_tag != "manual"))
        # Snapshot-derived filters: manual leads have no snapshot, so they bypass
        # these rather than being structurally excluded from every filtered view.
        if categories:
            q = q.filter(or_(is_manual, CrawlSnapshot.category_code.in_(categories)))
        if states:
            q = q.filter(or_(is_manual, CrawlSnapshot.state.in_(states)))
        if org_types:
            q = q.filter(or_(is_manual, CrawlSnapshot.domain_org_type.in_(org_types)))
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
        if require_name:
            q = q.filter(Lead.person_name.isnot(None), Lead.person_name != "")
        if require_designation:
            q = q.filter(Lead.designation.isnot(None), Lead.designation != "")
        if require_phone:
            q = q.filter(Lead.phone.isnot(None), Lead.phone != "")
        if min_score is not None:
            # Manual leads are always score 0 by design; the points threshold
            # applies only to extracted leads, never to manual visibility.
            q = q.filter(or_(is_manual, Lead.lead_score >= min_score))
        return q

    @staticmethod
    def _apply_lead_sort(q, sort_by=None, sort_dir="desc"):
        ascending = sort_dir == "asc"
        has_phone = case((and_(Lead.phone.isnot(None), Lead.phone != ""), 1), else_=0)
        has_name = case((and_(Lead.person_name.isnot(None), Lead.person_name != ""), 1), else_=0)
        column = {"score": Lead.lead_score, "contact": has_phone, "name": has_name}.get(sort_by)
        if column is None:
            return q.order_by(Lead.captured_at.desc())
        return q.order_by(column.asc() if ascending else column.desc(), Lead.captured_at.desc())

    def get_leads(self, job_ids: list[int] | None = None, categories: list[str] = None,
                  states: list[str] = None, search: str = None, page: int = 1,
                  limit: int = 100, complete_only: bool = False,
                  min_score: int | None = None, org_types: list[str] = None,
                  entry_type: str = "both", require_name: bool = False,
                  require_designation: bool = False, require_phone: bool = False,
                  sort_by: str = None, sort_dir: str = "desc") -> tuple[list[dict], int]:
        with self._Session() as s:
            q = (
                s.query(Lead, CrawlSnapshot.title.label("domain_title"),
                        CrawlSnapshot.category_code)
                .outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            )
            q = self._apply_lead_filters(
                q, job_ids, categories, states, search, complete_only, min_score,
                org_types=org_types, entry_type=entry_type, require_name=require_name,
                require_designation=require_designation, require_phone=require_phone,
            )

            total = q.count()
            offset = (page - 1) * limit
            q = self._apply_lead_sort(q, sort_by, sort_dir)
            rows = q.offset(offset).limit(limit).all()
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

    def get_lead_ids(self, job_ids: list[int] | None = None, categories: list[str] = None,
                     states: list[str] = None, search: str = None,
                     complete_only: bool = False, min_score: int | None = None,
                     org_types: list[str] = None, entry_type: str = "both",
                     require_name: bool = False, require_designation: bool = False,
                     require_phone: bool = False) -> list[int]:
        with self._Session() as s:
            q = s.query(Lead.id).outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            q = self._apply_lead_filters(
                q, job_ids, categories, states, search, complete_only, min_score,
                org_types=org_types, entry_type=entry_type, require_name=require_name,
                require_designation=require_designation, require_phone=require_phone,
            )
            return [r[0] for r in q.all()]

    def get_all_leads_for_export(self, job_ids: list[int] | None = None,
                                 categories: list[str] = None, states: list[str] = None,
                                 search: str = None, lead_ids: list[int] = None,
                                 complete_only: bool = False,
                                 min_score: int | None = None, org_types: list[str] = None,
                                 entry_type: str = "both", require_name: bool = False,
                                 require_designation: bool = False,
                                 require_phone: bool = False) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Lead, CrawlSnapshot.title.label("domain_title"),
                        CrawlSnapshot.category_code, CrawlSnapshot.category_title)
                .outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            )
            if lead_ids:
                q = q.filter(Lead.id.in_(lead_ids))
            else:
                q = self._apply_lead_filters(
                    q, job_ids, categories, states, search, complete_only, min_score,
                    org_types=org_types, entry_type=entry_type, require_name=require_name,
                    require_designation=require_designation, require_phone=require_phone,
                )
            # domain_id (soft link, recovered from the snapshot at save time) groups
            # the same org together across jobs; snapshot_id would split it per-job.
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

    def get_lead_categories(self, job_ids: list[int] | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(CrawlSnapshot.category_code, CrawlSnapshot.category_title,
                        func.count(Lead.id).label("count"))
                .join(Lead, Lead.snapshot_id == CrawlSnapshot.id)
            )
            if job_ids:
                q = q.filter(Lead.job_id.in_(job_ids))
            rows = (
                q.group_by(CrawlSnapshot.category_code, CrawlSnapshot.category_title)
                .order_by(func.count(Lead.id).desc())
                .all()
            )
            return [
                {"code": r.category_code,
                 "title": r.category_title or r.category_code,
                 "count": r.count}
                for r in rows
            ]

    def get_lead_org_types(self, job_ids: list[int] | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(CrawlSnapshot.org_type, CrawlSnapshot.org_type_title,
                        func.count(Lead.id).label("count"))
                .join(Lead, Lead.snapshot_id == CrawlSnapshot.id)
                .filter(CrawlSnapshot.org_type.isnot(None))
            )
            if job_ids:
                q = q.filter(Lead.job_id.in_(job_ids))
            rows = (
                q.group_by(CrawlSnapshot.org_type, CrawlSnapshot.org_type_title)
                .order_by(func.count(Lead.id).desc())
                .all()
            )
            return [
                {"code": r.org_type,
                 "title": r.org_type_title or r.org_type,
                 "count": r.count}
                for r in rows
            ]

    def get_lead_states(self, job_ids: list[int] | None = None, categories: list[str] = None) -> list[str]:
        with self._Session() as s:
            q = s.query(CrawlSnapshot.state).join(Lead, Lead.snapshot_id == CrawlSnapshot.id).filter(CrawlSnapshot.state.isnot(None))
            if job_ids:
                q = q.filter(Lead.job_id.in_(job_ids))
            if categories:
                q = q.filter(CrawlSnapshot.category_code.in_(categories))
            rows = q.distinct().order_by(CrawlSnapshot.state).all()
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
