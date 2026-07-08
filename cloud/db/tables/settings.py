import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String

from ..base import Base


class AppSetting(Base):
    """Generic cloud-side key/value settings store. One row this phase:
    key='crawl_policy' (plan.md §19.1 Phase 8 / §3.2) — the extraction,
    lead-score, and crawl-filter values that must be identical across every
    crawler. Machine-local runtime knobs (workers, timeouts, bind) stay in
    config.yaml, never here."""
    __tablename__ = "app_settings"
    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, nullable=False,
                         default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
