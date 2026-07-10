import json
from sqlalchemy import and_, case, false, func, or_
from sqlalchemy.exc import IntegrityError

from shared.scoring import compute_lead_score
from ..domain_resolution import resolve_domain_for_url
from ..tables.crawl import CrawlSnapshot
from ..tables.leads import Lead, LeadOccurrence

# Fields that a re-capture may fill in if the existing lead has them blank.
# Never overwrites an already-populated value (enrich, not replace).
# confidence_band/field_provenance are handled separately (see _BAND_RANK
# below) — a re-capture with objectively better evidence should UPGRADE the
# stored band, not just fill it if null (WI-9 Bug B).
_ENRICHABLE_FIELDS = (
    "person_name",
    "designation",
    "department",
    "source_title",
    "context_snippet",
    "phone",
    "entity_kind",
)

# confidence_band ordering for the upgrade-if-better comparison below (the
# parser emits only HIGH/LOW). Kept cloud-side rather than importing the
# agent tier's rung ranking — the cloud tier must never import agent.* (see
# pyproject.toml's import-linter contracts).
_BAND_RANK = {"LOW": 0, "HIGH": 1}

# WI-5 (PLAN_attribution_and_parser.md): a crawled lead whose source_url
# resolved to no catalog domain (see save_lead's WI-4 attribution) must stay
# findable rather than silently always/never showing up in filters — surfaced
# as an explicit "Unknown" sentinel in the state/category/org-type facets.
UNKNOWN = "Unknown"
UNKNOWN_CODE = "__unknown__"


def _unattributed_predicate():
    """A crawled (non-manual) lead the system couldn't attribute to any
    catalog domain — snapshot_id is NULL for a reason other than being a
    manual/CSV-imported lead."""
    return and_(Lead.snapshot_id.is_(None), or_(Lead.channel_tag.is_(None), Lead.channel_tag != "manual"))


def _department_is_url_fallback(field_provenance: str | None) -> bool:
    """True when the parser's only source for `department` was the bare
    URL subdomain slug (e.g. "seepz"), not anything found on the page —
    see parser.py's `_score` stage, which tags this as "url_default"."""
    if not field_provenance:
        return False
    try:
        provenance = json.loads(field_provenance)
    except (TypeError, ValueError):
        return False
    return provenance.get("department") == "url_default"


class LeadMixin:
    def get_lead_score_weights(self) -> dict:
        return self._lead_score_weights

    def bulk_save_leads(self, items: list[dict], captured_by: int | None = None) -> list[bool]:
        """Batch wrapper for the coordination `/leads` endpoint — one save_lead
        call per item, in submission order, so the caller can report per-item
        accepted/duplicate flags back to the agent."""
        return [self.save_lead(captured_by=captured_by, **item) for item in items]

    def _record_occurrence(
        self, s, lead_id: int, job_id: int, source_url: str | None, captured_by: int | None = None
    ) -> None:
        """Get-or-create a lead_occurrences row (unique lead_id+job_id). Tolerates
        a race/duplicate call the same way create_crawl_snapshot does."""
        try:
            s.add(LeadOccurrence(lead_id=lead_id, job_id=job_id, source_url=source_url, captured_by=captured_by))
            s.commit()
        except IntegrityError:
            s.rollback()  # already recorded for this (lead, job) pair

    def save_lead(
        self,
        job_id: int,
        snapshot_id: int | None,
        email: str | None,
        person_name: str | None,
        designation: str | None,
        department: str | None,
        source_url: str,
        source_title: str | None,
        context_snippet: str,
        entity_kind: str | None = None,
        phone: str | None = None,
        channel_tag: str | None = None,
        confidence_band: str | None = None,
        field_provenance: str | None = None,
        depth: int = 0,
        captured_by: int | None = None,
    ) -> bool:
        if not email:
            return False
        email = email.lower()
        with self._Session() as s:
            existing = s.query(Lead).filter(Lead.email == email).first()
            if existing:
                # Enrich-on-conflict: fill nulls only, never overwrite a
                # populated field, then record this job's capture.
                candidates = {
                    "person_name": person_name,
                    "designation": designation,
                    "department": department,
                    "source_title": source_title,
                    "context_snippet": context_snippet,
                    "phone": phone,
                    "entity_kind": entity_kind,
                }
                changed = False
                for field in _ENRICHABLE_FIELDS:
                    value = candidates.get(field)
                    if value and not getattr(existing, field):
                        setattr(existing, field, value)
                        changed = True

                # Upgrade-if-better, not fill-if-null: a later capture with
                # objectively stronger evidence (e.g. this crawl found a real
                # mailto: link where an earlier pass only scraped page text)
                # should replace the stored band — the first capture's band
                # is otherwise kept forever even after better evidence shows
                # up (issue #58). Never downgrades.
                if confidence_band and _BAND_RANK.get(confidence_band, -1) > _BAND_RANK.get(
                    existing.confidence_band, -1
                ):
                    existing.confidence_band = confidence_band
                    if field_provenance:
                        existing.field_provenance = field_provenance
                    changed = True

                if changed:
                    existing.lead_score = compute_lead_score(
                        {
                            "email": existing.email,
                            "phone": existing.phone,
                            "person_name": existing.person_name,
                            "designation": existing.designation,
                        },
                        confidence_band=existing.confidence_band,
                        channel_tag=existing.channel_tag,
                        weights=self._lead_score_weights,
                    )
                    s.commit()
                self._record_occurrence(s, existing.id, job_id, source_url, captured_by)
                return True

            # Domain attribution (WI-4): a crawl started at one seed can follow
            # links into a different catalog domain and capture a lead there —
            # resolve the ACTUAL domain from source_url rather than trusting
            # the inherited seed snapshot_id.
            resolved = resolve_domain_for_url(
                source_url,
                self._get_netloc_domain_map(),
                self.get_crawl_policy().get("crawler", {}).get("target_suffixes", [".gov.in", ".nic.in"]),
            )
            if resolved is None:
                # No catalog domain matches this URL at all — leave unattributed
                # ("Unknown" in the UI, see WI-5) rather than keep a possibly-
                # wrong inherited snapshot.
                snapshot_id = None
            else:
                inherited_source_domain_id = None
                if snapshot_id is not None:
                    inherited = s.query(CrawlSnapshot.source_domain_id).filter_by(id=snapshot_id).first()
                    inherited_source_domain_id = inherited.source_domain_id if inherited else None
                if resolved["id"] != inherited_source_domain_id:
                    snapshot_id = self.create_crawl_snapshot(job_id, resolved, is_seed=False)
                # else: resolves to the domain the inherited snapshot already
                # describes — keep snapshot_id as-is.

            # A department that came only from the URL's bare subdomain slug
            # is a worse label than the org's actual title — swap it in when
            # nothing on the page itself supplied a department.
            if snapshot_id and _department_is_url_fallback(field_provenance):
                snap = s.query(CrawlSnapshot.title).filter_by(id=snapshot_id).first()
                if snap and snap.title:
                    department = snap.title

            lead_score = compute_lead_score(
                {
                    "email": email,
                    "phone": phone,
                    "person_name": person_name,
                    "designation": designation,
                },
                confidence_band=confidence_band,
                channel_tag=channel_tag,
                weights=self._lead_score_weights,
            )
            try:
                lead = Lead(
                    job_id=job_id,
                    snapshot_id=snapshot_id,
                    email=email,
                    person_name=person_name,
                    designation=designation,
                    department=department,
                    source_url=source_url,
                    source_title=source_title,
                    context_snippet=context_snippet,
                    entity_kind=entity_kind,
                    phone=phone,
                    channel_tag=channel_tag,
                    confidence_band=confidence_band,
                    field_provenance=field_provenance,
                    lead_score=lead_score,
                    depth=depth,
                )
                s.add(lead)
                s.commit()
                self._record_occurrence(s, lead.id, job_id, source_url, captured_by)
                return True
            except IntegrityError:
                s.rollback()
                return False

    @staticmethod
    def _apply_lead_filters(
        q,
        job_ids=None,
        categories=None,
        states=None,
        search=None,
        complete_only=False,
        min_score=None,
        org_types=None,
        entry_type="both",
        require_name=False,
        require_designation=False,
        require_phone=False,
    ):
        is_manual = Lead.channel_tag == "manual"
        if job_ids:
            q = q.filter(Lead.job_id.in_(job_ids))
        if entry_type == "manual":
            q = q.filter(Lead.channel_tag == "manual")
        elif entry_type == "extracted":
            q = q.filter(or_(Lead.channel_tag.is_(None), Lead.channel_tag != "manual"))
        # Snapshot-derived filters: manual leads have no snapshot, so they bypass
        # these rather than being structurally excluded from every filtered view.
        # Requesting the UNKNOWN/UNKNOWN_CODE sentinel additionally admits
        # unattributed crawled leads (WI-5) — a null-snapshot lead that isn't
        # manual either.
        if categories:
            unknown = _unattributed_predicate() if UNKNOWN_CODE in categories else false()
            q = q.filter(or_(is_manual, CrawlSnapshot.category_code.in_(categories), unknown))
        if states:
            unknown = _unattributed_predicate() if UNKNOWN in states else false()
            q = q.filter(or_(is_manual, CrawlSnapshot.state.in_(states), Lead.manual_state.in_(states), unknown))
        if org_types:
            unknown = _unattributed_predicate() if UNKNOWN_CODE in org_types else false()
            q = q.filter(or_(is_manual, CrawlSnapshot.org_type.in_(org_types), unknown))
        if search:
            q = q.filter(
                or_(
                    Lead.email.ilike(f"%{search}%"),
                    Lead.person_name.ilike(f"%{search}%"),
                    Lead.department.ilike(f"%{search}%"),
                    Lead.designation.ilike(f"%{search}%"),
                )
            )
        if complete_only:
            q = q.filter(
                Lead.person_name.isnot(None),
                Lead.person_name != "",
                Lead.designation.isnot(None),
                Lead.designation != "",
                Lead.department.isnot(None),
                Lead.department != "",
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

    def get_leads(
        self,
        job_ids: list[int] | None = None,
        categories: list[str] = None,
        states: list[str] = None,
        search: str = None,
        page: int = 1,
        limit: int = 100,
        complete_only: bool = False,
        min_score: int | None = None,
        org_types: list[str] = None,
        entry_type: str = "both",
        require_name: bool = False,
        require_designation: bool = False,
        require_phone: bool = False,
        sort_by: str = None,
        sort_dir: str = "desc",
    ) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = s.query(
                Lead,
                CrawlSnapshot.title.label("domain_title"),
                CrawlSnapshot.category_code,
                CrawlSnapshot.state.label("snap_state"),
                CrawlSnapshot.org_type_title,
            ).outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            q = self._apply_lead_filters(
                q,
                job_ids,
                categories,
                states,
                search,
                complete_only,
                min_score,
                org_types=org_types,
                entry_type=entry_type,
                require_name=require_name,
                require_designation=require_designation,
                require_phone=require_phone,
            )

            total = q.count()
            offset = (page - 1) * limit
            q = self._apply_lead_sort(q, sort_by, sort_dir)
            rows = q.offset(offset).limit(limit).all()
            return (
                [
                    {
                        "id": lead.id,
                        "email": lead.email,
                        "person_name": lead.person_name,
                        "designation": lead.designation,
                        "department": lead.department,
                        "source_url": lead.source_url,
                        "source_title": lead.source_title,
                        "context_snippet": lead.context_snippet,
                        "domain_title": dt,
                        "category_code": cc,
                        # display-only: manual leads show manual_state; a crawled
                        # lead shows its snapshot's frozen state, or "Unknown" if
                        # source_url couldn't be attributed to any catalog domain
                        # (never falls through to manual_state — see WI-5).
                        "domain_state": lead.manual_state if lead.channel_tag == "manual" else (snap_state or UNKNOWN),
                        "domain_org_type": ot,
                        "manual_state": lead.manual_state,
                        "is_manual": lead.channel_tag == "manual",
                        "confidence_band": lead.confidence_band,
                        "field_provenance": lead.field_provenance,
                        "channel_tag": lead.channel_tag,
                        "phone": lead.phone,
                        "lead_score": lead.lead_score or 0,
                        "depth": lead.depth or 0,
                        "captured_at": lead.captured_at.isoformat() if lead.captured_at else None,
                    }
                    for lead, dt, cc, snap_state, ot in rows
                ],
                total,
            )

    def get_lead_ids(
        self,
        job_ids: list[int] | None = None,
        categories: list[str] = None,
        states: list[str] = None,
        search: str = None,
        complete_only: bool = False,
        min_score: int | None = None,
        org_types: list[str] = None,
        entry_type: str = "both",
        require_name: bool = False,
        require_designation: bool = False,
        require_phone: bool = False,
    ) -> list[int]:
        with self._Session() as s:
            q = s.query(Lead.id).outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            q = self._apply_lead_filters(
                q,
                job_ids,
                categories,
                states,
                search,
                complete_only,
                min_score,
                org_types=org_types,
                entry_type=entry_type,
                require_name=require_name,
                require_designation=require_designation,
                require_phone=require_phone,
            )
            return [r[0] for r in q.all()]

    def get_all_leads_for_export(
        self,
        job_ids: list[int] | None = None,
        categories: list[str] = None,
        states: list[str] = None,
        search: str = None,
        lead_ids: list[int] = None,
        complete_only: bool = False,
        min_score: int | None = None,
        org_types: list[str] = None,
        entry_type: str = "both",
        require_name: bool = False,
        require_designation: bool = False,
        require_phone: bool = False,
    ) -> list[dict]:
        with self._Session() as s:
            q = s.query(
                Lead,
                CrawlSnapshot.title.label("domain_title"),
                CrawlSnapshot.category_code,
                CrawlSnapshot.category_title,
                CrawlSnapshot.state.label("snap_state"),
                CrawlSnapshot.org_type_title,
                CrawlSnapshot.source_domain_id,
            ).outerjoin(CrawlSnapshot, Lead.snapshot_id == CrawlSnapshot.id)
            if lead_ids:
                q = q.filter(Lead.id.in_(lead_ids))
            else:
                q = self._apply_lead_filters(
                    q,
                    job_ids,
                    categories,
                    states,
                    search,
                    complete_only,
                    min_score,
                    org_types=org_types,
                    entry_type=entry_type,
                    require_name=require_name,
                    require_designation=require_designation,
                    require_phone=require_phone,
                )
            # source_domain_id (via the snapshot) groups the same org together
            # across jobs; snapshot_id would split it per-job. Manual leads
            # (no snapshot) sort after, grouped by nothing in particular.
            rows = q.order_by(CrawlSnapshot.source_domain_id, Lead.captured_at).all()
            return [
                {
                    "email": lead.email,
                    "person_name": lead.person_name or "",
                    "designation": lead.designation or "",
                    "department": lead.department or "",
                    "domain_title": dt or "",
                    "domain_state": (lead.manual_state if lead.channel_tag == "manual" else (snap_state or UNKNOWN))
                    or "",
                    "domain_org_type": ot or "",
                    "category_title": ct or cc or "",
                    "source_url": lead.source_url or "",
                    "source_title": lead.source_title or "",
                    "context_snippet": lead.context_snippet or "",
                    "confidence_band": lead.confidence_band or "",
                    "field_provenance": lead.field_provenance or "",
                    "phone": lead.phone or "",
                    "lead_score": lead.lead_score or 0,
                    "depth": lead.depth or 0,
                    "captured_at": lead.captured_at.isoformat() if lead.captured_at else "",
                }
                for lead, dt, cc, ct, snap_state, ot, _sdi in rows
            ]

    def _unattributed_count(self, s, job_ids: list[int] | None = None) -> int:
        q = s.query(func.count(Lead.id)).filter(_unattributed_predicate())
        if job_ids:
            q = q.filter(Lead.job_id.in_(job_ids))
        return q.scalar() or 0

    def get_lead_categories(self, job_ids: list[int] | None = None) -> list[dict]:
        with self._Session() as s:
            q = s.query(
                CrawlSnapshot.category_code, CrawlSnapshot.category_title, func.count(Lead.id).label("count")
            ).join(Lead, Lead.snapshot_id == CrawlSnapshot.id)
            if job_ids:
                q = q.filter(Lead.job_id.in_(job_ids))
            rows = (
                q.group_by(CrawlSnapshot.category_code, CrawlSnapshot.category_title)
                .order_by(func.count(Lead.id).desc())
                .all()
            )
            result = [
                {"code": r.category_code, "title": r.category_title or r.category_code, "count": r.count} for r in rows
            ]
            unknown_count = self._unattributed_count(s, job_ids)
            if unknown_count:
                result.append({"code": UNKNOWN_CODE, "title": UNKNOWN, "count": unknown_count})
            return result

    def get_lead_org_types(self, job_ids: list[int] | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(CrawlSnapshot.org_type, CrawlSnapshot.org_type_title, func.count(Lead.id).label("count"))
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
            result = [{"code": r.org_type, "title": r.org_type_title or r.org_type, "count": r.count} for r in rows]
            unknown_count = self._unattributed_count(s, job_ids)
            if unknown_count:
                result.append({"code": UNKNOWN_CODE, "title": UNKNOWN, "count": unknown_count})
            return result

    def get_lead_states(self, job_ids: list[int] | None = None, categories: list[str] = None) -> list[str]:
        with self._Session() as s:
            q = (
                s.query(CrawlSnapshot.state)
                .join(Lead, Lead.snapshot_id == CrawlSnapshot.id)
                .filter(CrawlSnapshot.state.isnot(None))
            )
            if job_ids:
                q = q.filter(Lead.job_id.in_(job_ids))
            if categories:
                q = q.filter(CrawlSnapshot.category_code.in_(categories))
            rows = q.distinct().order_by(CrawlSnapshot.state).all()
            states = {r[0] for r in rows if r[0]}
            with self._Session() as s2:
                manual_q = s2.query(Lead.manual_state).filter(Lead.manual_state.isnot(None))
                if job_ids:
                    manual_q = manual_q.filter(Lead.job_id.in_(job_ids))
                states.update(r[0] for r in manual_q.distinct().all() if r[0])
            if self._unattributed_count(s, job_ids):
                states.add(UNKNOWN)
            return sorted(states)

    def bulk_upsert_manual_leads(
        self, job_id: int, rows: list[dict], captured_by: int | None = None
    ) -> tuple[int, int, list[dict]]:
        """Insert/update CSV-uploaded leads. Updates a row only if the existing
        lead with that email is itself manual; a crawled lead is left untouched
        but still gets a lead_occurrences row recording this capture."""
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
                        existing.lead_score = compute_lead_score(
                            {
                                "email": existing.email,
                                "phone": existing.phone,
                                "person_name": existing.person_name,
                                "designation": existing.designation,
                            },
                            confidence_band=existing.confidence_band,
                            channel_tag=existing.channel_tag,
                            weights=self._lead_score_weights,
                        )
                        s.commit()
                        self._record_occurrence(s, existing.id, job_id, "manual-csv-upload", captured_by)
                        updated += 1
                    else:
                        self._record_occurrence(s, existing.id, job_id, "manual-csv-upload", captured_by)
                        skipped.append(
                            {
                                "row": row.get("row"),
                                "email": row["email"],
                                "reason": "email already exists as a crawled lead",
                            }
                        )
                    continue
                lead_score = compute_lead_score(
                    {
                        "email": row["email"],
                        "phone": row.get("phone"),
                        "person_name": row.get("name"),
                        "designation": row.get("designation"),
                    },
                    confidence_band=None,
                    channel_tag="manual",
                    weights=self._lead_score_weights,
                )
                lead = Lead(
                    job_id=job_id,
                    email=row["email"],
                    person_name=row.get("name"),
                    designation=row.get("designation"),
                    department=row.get("department"),
                    source_url="manual-csv-upload",
                    source_title=None,
                    context_snippet=None,
                    phone=row.get("phone"),
                    channel_tag="manual",
                    lead_score=lead_score,
                    depth=0,
                )
                s.add(lead)
                s.commit()
                self._record_occurrence(s, lead.id, job_id, "manual-csv-upload", captured_by)
                imported += 1
        return imported, updated, skipped

    _LEAD_EDITABLE = frozenset({"person_name", "designation", "department", "manual_state"})

    def update_lead(self, lead_id: int, updates: dict) -> bool | str:
        """Returns True on success, False if the lead doesn't exist, or the
        string "not_manual" if a manual_state edit was attempted on a crawled
        lead (its state is derived from the crawl snapshot, not editable)."""
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
            if "manual_state" in safe and lead.channel_tag != "manual":
                return "not_manual"
            for k, v in safe.items():
                setattr(lead, k, v)
            lead.lead_score = compute_lead_score(
                {
                    "email": lead.email,
                    "phone": lead.phone,
                    "person_name": lead.person_name,
                    "designation": lead.designation,
                },
                confidence_band=lead.confidence_band,
                channel_tag=lead.channel_tag,
                weights=self._lead_score_weights,
            )
            s.commit()
            return True
