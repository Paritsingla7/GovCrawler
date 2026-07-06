from sqlalchemy import case, func, or_

from ..tables.crawl import Domain


class DomainMixin:
    def upsert_domain(self, category_code: str, category_title: str,
                      state: str, org_type: str, org_type_title: str,
                      title: str, main_url: str | None, contact_url: str | None,
                      external_id: str | None = None) -> int:
        """Dedupe by external_id when available (the only stable key for
        entries without a main_url); otherwise fall back to main_url. Entries
        with neither (main_url is None and no external_id) are always
        inserted fresh — there's no reliable key to match them against.
        """
        with self._Session() as s:
            existing = None
            if external_id:
                existing = s.query(Domain).filter_by(external_id=external_id).first()
            elif main_url:
                existing = s.query(Domain).filter_by(main_url=main_url).first()

            if existing:
                existing.category_code = category_code
                existing.category_title = category_title
                existing.state = state
                existing.org_type = org_type
                existing.org_type_title = org_type_title
                existing.title = title
                existing.main_url = main_url
                existing.contact_url = contact_url
                existing.external_id = external_id
                s.commit()
                return existing.id
            d = Domain(
                category_code=category_code, category_title=category_title,
                state=state, org_type=org_type, org_type_title=org_type_title,
                title=title, main_url=main_url, contact_url=contact_url,
                external_id=external_id,
            )
            s.add(d)
            s.commit()
            return d.id

    def update_domain_url(self, domain_id: int, main_url: str,
                          contact_url: str | None = None) -> dict | None:
        """Manually set a crawlable URL on a domain that was imported without one."""
        with self._Session() as s:
            d = s.query(Domain).filter_by(id=domain_id).first()
            if not d:
                return None
            d.main_url = main_url
            if contact_url is not None:
                d.contact_url = contact_url
            s.commit()
            return {"id": d.id, "title": d.title, "main_url": d.main_url,
                    "contact_url": d.contact_url, "category_code": d.category_code,
                    "state": d.state, "org_type": d.org_type}

    def clear_domains(self):
        with self._Session() as s:
            s.query(Domain).delete()
            s.commit()

    def count_domains(self) -> int:
        with self._Session() as s:
            return s.query(Domain).count()

    def get_domain_stats(self, category: str = None, state: str = None,
                         org_type: str = None, search: str = None) -> dict:
        """Total / crawlable / duplicate counts for domains matching the given
        filters (same filters as get_domains — pass none for the whole table).
        `duplicate` counts rows sharing a main_url with another row *within
        this filtered set*, minus one per group (the redundant extra rows,
        not the whole group).
        """

        def _filtered(q):
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if org_type:
                q = q.filter(Domain.org_type == org_type)
            if search:
                q = q.filter(
                    or_(Domain.title.ilike(f"%{search}%"),
                        Domain.main_url.ilike(f"%{search}%"))
                )
            return q

        with self._Session() as s:
            total = _filtered(s.query(Domain)).count()
            crawlable = _filtered(s.query(Domain).filter(Domain.main_url.isnot(None))).count()

            dup_groups = (
                _filtered(s.query(Domain.main_url, func.count(Domain.id).label("cnt"))
                          .filter(Domain.main_url.isnot(None)))
                .group_by(Domain.main_url)
                .having(func.count(Domain.id) > 1)
                .subquery()
            )
            duplicate = s.query(func.coalesce(func.sum(dup_groups.c.cnt - 1), 0)).scalar()

            return {
                "total": total,
                "crawlable": crawlable,
                "not_crawlable": total - crawlable,
                "duplicate": int(duplicate),
            }

    def get_categories(self) -> list[dict]:
        with self._Session() as s:
            rows = (
                s.query(Domain.category_code, Domain.category_title,
                        func.count(Domain.id).label("count"))
                .group_by(Domain.category_code, Domain.category_title)
                .order_by(func.count(Domain.id).desc())
                .all()
            )
            return [
                {"code": r.category_code,
                 "title": r.category_title or r.category_code,
                 "count": r.count}
                for r in rows
            ]

    def get_states(self, category: str = None) -> list[str]:
        with self._Session() as s:
            q = s.query(Domain.state).filter(Domain.state.isnot(None))
            if category:
                q = q.filter(Domain.category_code == category)
            rows = q.distinct().order_by(Domain.state).all()
            return [r[0] for r in rows if r[0]]

    def get_org_types(self, category: str = None, state: str = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Domain.org_type, Domain.org_type_title,
                        func.count(Domain.id).label("count"))
                .filter(Domain.org_type.isnot(None))
            )
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            rows = (
                q.group_by(Domain.org_type, Domain.org_type_title)
                .order_by(func.count(Domain.id).desc())
                .all()
            )
            return [
                {"code": r.org_type,
                 "title": r.org_type_title or r.org_type,
                 "count": r.count}
                for r in rows
            ]

    def get_domains(self, category: str = None, state: str = None,
                    org_type: str = None, search: str = None,
                    page: int = 1, limit: int = 50,
                    sort_by: str = None, sort_dir: str = "desc") -> tuple[list[dict], int]:
        with self._Session() as s:
            q = s.query(Domain)
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if org_type:
                q = q.filter(Domain.org_type == org_type)
            if search:
                # Search title OR the domain URL so it works even when title is empty
                q = q.filter(
                    or_(Domain.title.ilike(f"%{search}%"),
                        Domain.main_url.ilike(f"%{search}%"))
                )
            total = q.count()
            offset = (page - 1) * limit
            if sort_by == "crawlable":
                is_crawlable = case((Domain.main_url.isnot(None), 1), else_=0)
                order = is_crawlable.asc() if sort_dir == "asc" else is_crawlable.desc()
                q = q.order_by(order, Domain.state, Domain.main_url)
            else:
                q = q.order_by(Domain.state, Domain.main_url)
            rows = q.offset(offset).limit(limit).all()
            return (
                [{"id": d.id, "category_code": d.category_code,
                  "category_title": d.category_title, "state": d.state,
                  "org_type": d.org_type, "org_type_title": d.org_type_title,
                  "title": d.title, "main_url": d.main_url,
                  "contact_url": d.contact_url}
                 for d in rows],
                total,
            )

    def get_domain_ids(self, category: str = None, state: str = None,
                       org_type: str = None, search: str = None) -> list[int]:
        """Return matching, crawlable (main_url is set) domain IDs — used by
        select-all in the UI. Domains with no URL are excluded since they
        can't be used as crawl seeds.
        """
        with self._Session() as s:
            q = s.query(Domain.id).filter(Domain.main_url.isnot(None))
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if org_type:
                q = q.filter(Domain.org_type == org_type)
            if search:
                q = q.filter(
                    or_(Domain.title.ilike(f"%{search}%"),
                        Domain.main_url.ilike(f"%{search}%"))
                )
            return [r[0] for r in q.all()]

    def get_domains_by_ids(self, ids: list[int]) -> list[dict]:
        with self._Session() as s:
            rows = s.query(Domain).filter(Domain.id.in_(ids)).all()
            return [
                {"id": d.id, "title": d.title, "main_url": d.main_url,
                 "contact_url": d.contact_url, "category_code": d.category_code,
                 "state": d.state, "org_type": d.org_type}
                for d in rows
            ]
