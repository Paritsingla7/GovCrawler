import enum


class CampaignStatus(enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class EmailStatus(enum.Enum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
