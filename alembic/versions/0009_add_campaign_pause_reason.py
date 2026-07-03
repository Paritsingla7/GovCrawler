"""Add pause_reason to campaigns and test_campaigns

Revision ID: 0009_add_campaign_pause_reason
Revises: 0008_add_campaign_credentials
"""
from alembic import op
import sqlalchemy as sa

revision = '0009_add_campaign_pause_reason'
down_revision = '0008_add_campaign_credentials'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaigns', sa.Column('pause_reason', sa.String(), nullable=True))
    op.add_column('test_campaigns', sa.Column('pause_reason', sa.String(), nullable=True))


def downgrade():
    op.drop_column('test_campaigns', 'pause_reason')
    op.drop_column('campaigns', 'pause_reason')
