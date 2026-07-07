import enum


class CampaignStatus(enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class CampaignKind(enum.Enum):
    """TEXT+CHECK on the DB side (not a native SqlEnum column, per plan.md §4's
    portability rationale — ALTER TYPE is transaction-hostile on Postgres and
    can't drop values), but kept as a real enum in app code for type safety."""
    PRODUCTION = "production"
    TEST = "test"


class EmailStatus(enum.Enum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


class JobStatus(enum.Enum):
    """Promotes the plain crawl_jobs.status strings to a real enum.

    Values == names so existing rows (plain lowercase strings written before
    this enum existed) still compare equal to JobStatus(x).value.
    """
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    MANUAL_UPLOAD = "manual_upload"
