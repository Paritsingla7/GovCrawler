"""Re-export of the campaign/email enums (canonical home: shared/enums.py) so
`cloud/db/tables/outreach.py`'s `from ..enums import ...` keeps working."""
from shared.enums import CampaignKind, CampaignStatus, EmailStatus, JobStatus

__all__ = ["CampaignKind", "CampaignStatus", "EmailStatus", "JobStatus"]
