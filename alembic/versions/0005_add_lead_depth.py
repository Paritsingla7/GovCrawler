"""Add depth column to leads

Revision ID: 0005_add_lead_depth
Revises: 0004_add_email_selection
"""
from alembic import op
import sqlalchemy as sa

revision = '0005_add_lead_depth'
down_revision = '0004_add_email_selection'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'leads',
        sa.Column('depth', sa.Integer(), nullable=False, server_default='0')
    )


def downgrade():
    op.drop_column('leads', 'depth')
