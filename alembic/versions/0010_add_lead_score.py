"""Add lead_score to leads

Revision ID: 0010_add_lead_score
Revises: 0009_add_campaign_pause_reason
"""
from alembic import op
import sqlalchemy as sa

revision = '0010_add_lead_score'
down_revision = '0009_add_campaign_pause_reason'
branch_labels = None
depends_on = None


def upgrade():
    # Values are populated by Database._recompute_lead_scores() at app
    # startup (the actual schema-evolution path this app uses) rather than
    # here, so there's one implementation of the scoring rules, not two
    # that can drift.
    op.add_column('leads', sa.Column('lead_score', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('leads', 'lead_score')
