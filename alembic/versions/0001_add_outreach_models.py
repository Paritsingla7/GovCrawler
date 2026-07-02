"""Migration script to add outreach & campaign management models"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_add_outreach_models"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "PAUSED",
                "CANCELLED",
                "COMPLETED",
                name="campaignstatus",
            ),
            nullable=False,
        ),
    )
    op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("raw_body", sa.Text(), nullable=False),
    )
    op.create_table(
        "smtp_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "campaign_emails",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("recipient_email", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "QUEUED",
                "SENT",
                "FAILED",
                name="emailstatus",
            ),
            nullable=False,
            server_default="DRAFT",
        ),
    )
    op.create_table(
        "blacklist",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("domain", sa.String(), nullable=False, index=True),
        sa.Column("reason", sa.String(), nullable=True),
    )


def downgrade():
    op.drop_table("blacklist")
    op.drop_table("campaign_emails")
    op.drop_table("smtp_credentials")
    op.drop_table("email_templates")
    op.drop_table("campaigns")
