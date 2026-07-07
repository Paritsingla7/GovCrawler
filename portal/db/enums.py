"""Moved to shared/enums.py (Phase 0 — shared/ carve-out). Re-exported here
so `portal/db/tables/outreach.py`'s `from ..enums import ...` keeps working."""
from shared.enums import CampaignKind, CampaignStatus, EmailStatus

__all__ = ["CampaignKind", "CampaignStatus", "EmailStatus"]
