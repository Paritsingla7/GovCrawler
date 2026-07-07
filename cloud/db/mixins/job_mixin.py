import datetime
import json

from shared.enums import JobStatus

from ..tables.crawl import CrawlJob, CrawlJobDomain, JobCustomUrl, JobFrontier

_TERMINAL_STATUSES = {JobStatus.DONE.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}


class JobMixin:
    def create_job(self, domain_ids: list[int] = None, custom_urls: list[str] = None,
                   category_filter: str = None, title_filter: str = None,
                   owner_id: int | None = None) -> int:
        source_type = "custom_urls" if custom_urls else "domains"
        seed_count = len(custom_urls) if custom_urls else len(domain_ids or [])
        with self._Session() as s:
            job = CrawlJob(
                domain_ids=json.dumps(domain_ids or []),
                source_type=source_type,
                category_filter=category_filter,
                title_filter=title_filter,
                total_domains=seed_count,
                seed_domains=seed_count,
                status="pending",
                owner_id=owner_id,
            )
            s.add(job)
            s.commit()
            if domain_ids:
                s.add_all([CrawlJobDomain(job_id=job.id, domain_id=d) for d in domain_ids])
                s.commit()
            return job.id

    def get_job_domain_ids(self, job_id: int) -> list[int]:
        with self._Session() as s:
            return [r[0] for r in s.query(CrawlJobDomain.domain_id).filter_by(job_id=job_id).all()]

    def add_job_custom_urls(self, job_id: int, urls: list[str]) -> None:
        with self._Session() as s:
            s.add_all([JobCustomUrl(job_id=job_id, url=url) for url in urls])
            s.commit()

    def get_job_custom_urls(self, job_id: int) -> list[dict]:
        with self._Session() as s:
            rows = (
                s.query(JobCustomUrl)
                .filter_by(job_id=job_id)
                .order_by(JobCustomUrl.id)
                .all()
            )
            return [
                {"id": r.id, "title": r.url, "main_url": r.url,
                 "contact_url": None, "category_code": "custom", "state": None,
                 "org_type": None}
                for r in rows
            ]

    def start_job(self, job_id: int):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "status": "running",
                "started_at": datetime.datetime.utcnow(),
            })
            s.commit()

    def finish_job(self, job_id: int, status: str = "done", error: str = None):
        with self._Session() as s:
            job = s.query(CrawlJob).filter_by(id=job_id).first()
            if job is None or job.status in _TERMINAL_STATUSES:
                # Already finished by a racing path (in-memory cancel vs. a
                # heartbeat-driven finish) — a no-op, not an error.
                return
            s.query(CrawlJob).filter_by(id=job_id).update({
                "status": status,
                "finished_at": datetime.datetime.utcnow(),
                "error_message": error,
            })
            s.commit()

    def set_cancel_requested(self, job_id: int) -> None:
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({"cancel_requested": True})
            s.commit()

    def heartbeat(self, job_id: int, metrics: dict) -> bool:
        """Records a liveness pulse + the latest metrics; returns cancel_requested.

        Also reconciles a job the reaper marked `interrupted` back to
        `running` — a late heartbeat proves the agent wasn't actually dead,
        just slow, so this must be non-destructive (no data touched, just
        the status flip) per plan.md §10.6."""
        with self._Session() as s:
            updates = {
                "queued_urls": metrics.get("queued_urls", 0),
                "visited_urls": metrics.get("visited_urls", 0),
                "skipped_urls": metrics.get("skipped_urls", 0),
                "leads_found": metrics.get("leads_found", 0),
                "crawled_domains": metrics.get("crawled_domains", 0),
                "current_depth": metrics.get("current_depth", 0),
                "active_workers": metrics.get("active_workers", 0),
                "last_heartbeat_at": datetime.datetime.utcnow(),
            }
            job = s.query(CrawlJob).filter_by(id=job_id).first()
            if job and job.status == JobStatus.INTERRUPTED.value:
                updates["status"] = JobStatus.RUNNING.value
            s.query(CrawlJob).filter_by(id=job_id).update(updates)
            s.commit()
            job = s.query(CrawlJob).filter_by(id=job_id).first()
            return bool(job.cancel_requested) if job else False

    def reap_stale_jobs(self, threshold_seconds: int) -> list[int]:
        """Flips any 'running' job silent for longer than threshold_seconds to
        'interrupted' — non-destructive (heartbeat() revives it on a late
        pulse; a real resume also just clears cancel_requested/re-runs).
        Covers both a job with a stale last_heartbeat_at and one that never
        got a single heartbeat (started_at is the only signal there)."""
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=threshold_seconds)
        with self._Session() as s:
            stale = (
                s.query(CrawlJob.id)
                .filter(CrawlJob.status == JobStatus.RUNNING.value)
                .filter(
                    ((CrawlJob.last_heartbeat_at.isnot(None)) & (CrawlJob.last_heartbeat_at < cutoff)) |
                    ((CrawlJob.last_heartbeat_at.is_(None)) & (CrawlJob.started_at < cutoff))
                )
                .all()
            )
            ids = [r[0] for r in stale]
            if ids:
                s.query(CrawlJob).filter(CrawlJob.id.in_(ids)).update(
                    {"status": JobStatus.INTERRUPTED.value}, synchronize_session=False
                )
                s.commit()
            return ids

    def resume_job(self, job_id: int) -> None:
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "status": JobStatus.RUNNING.value,
                "cancel_requested": False,
            })
            s.commit()

    def get_or_create_manual_upload_job(self) -> int:
        """Shared synthetic job that all CSV-uploaded manual leads attach to."""
        with self._Session() as s:
            job = s.query(CrawlJob).filter_by(status="manual_upload").first()
            if job:
                return job.id
            job = CrawlJob(status="manual_upload", total_domains=0, seed_domains=0)
            s.add(job)
            s.commit()
            return job.id

    def get_job(self, job_id: int, owner_id: int | None = None, view_all: bool = False) -> dict | None:
        with self._Session() as s:
            q = s.query(CrawlJob).filter_by(id=job_id)
            if not view_all:
                q = q.filter(CrawlJob.owner_id == owner_id)
            j = q.first()
            return self._job_dict(j) if j else None

    def list_jobs(self, limit: int = 20, owner_id: int | None = None,
                  view_all: bool = False) -> list[dict]:
        with self._Session() as s:
            q = s.query(CrawlJob)
            if not view_all:
                q = q.filter(CrawlJob.owner_id == owner_id)
            rows = q.order_by(CrawlJob.created_at.desc()).limit(limit).all()
            return [self._job_dict(j) for j in rows]

    @staticmethod
    def _job_dict(j: CrawlJob) -> dict:
        return {
            "id": j.id, "status": j.status,
            "source_type": j.source_type,
            "total_domains": j.total_domains,
            "crawled_domains": j.crawled_domains,
            "seed_domains": j.seed_domains,
            "queued_urls": j.queued_urls,
            "visited_urls": j.visited_urls,
            "skipped_urls": j.skipped_urls,
            "leads_found": j.leads_found,
            "current_depth": j.current_depth or 0,
            "active_workers": j.active_workers or 0,
            "error_message": j.error_message,
            "owner_id": j.owner_id,
            "cancel_requested": bool(j.cancel_requested),
            "agent_hostname": j.agent_hostname,
            "last_heartbeat_at": j.last_heartbeat_at.isoformat() if j.last_heartbeat_at else None,
            "category_filter": j.category_filter,
            "title_filter": j.title_filter,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }

    def save_frontier_snapshot(self, job_id: int, snapshot: dict) -> None:
        """Cloud-side counterpart to agent/local_store.py's local frontier
        checkpoint — upserted, one row per job. Only called when
        crawler.cross_machine_resume is enabled (see agent/cloud_client.py)."""
        with self._Session() as s:
            payload = json.dumps(snapshot)
            existing = s.query(JobFrontier).filter_by(job_id=job_id).first()
            if existing:
                existing.snapshot_json = payload
                existing.updated_at = datetime.datetime.utcnow()
            else:
                s.add(JobFrontier(job_id=job_id, snapshot_json=payload,
                                  updated_at=datetime.datetime.utcnow()))
            s.commit()

    def load_frontier_snapshot(self, job_id: int) -> dict | None:
        with self._Session() as s:
            row = s.query(JobFrontier).filter_by(job_id=job_id).first()
            return json.loads(row.snapshot_json) if row else None
