import datetime

from sqlalchemy.exc import IntegrityError

from ..tables.crawl import VisitedUrl


class VisitedUrlMixin:
    def mark_visited(self, url: str, job_id: int):
        with self._Session() as s:
            try:
                s.add(VisitedUrl(url=url, job_id=job_id))
                s.commit()
            except IntegrityError:
                s.rollback()

    def get_visited_urls(self, job_id: int) -> set[str]:
        with self._Session() as s:
            rows = s.query(VisitedUrl.url).filter_by(job_id=job_id).all()
            return {r[0] for r in rows}

    def get_recently_visited_global(self) -> set[str]:
        """URLs visited in any job within the last recrawl_days — skip these in new jobs."""
        threshold = datetime.datetime.utcnow() - datetime.timedelta(days=self._recrawl_days)
        with self._Session() as s:
            rows = (
                s.query(VisitedUrl.url)
                .filter(VisitedUrl.visited_at >= threshold)
                .distinct()
                .all()
            )
            return {r[0] for r in rows}

    def clear_visited_urls(self):
        with self._Session() as s:
            s.query(VisitedUrl).delete()
            s.commit()
