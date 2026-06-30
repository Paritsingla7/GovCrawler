"""Add entity_kind, phone, channel_tag, confidence_band, field_provenance to leads

Revision ID: 0005_add_lead_grading
Revises: 0004_add_email_selection
"""
from alembic import op
import sqlalchemy as sa

revision = '0005_add_lead_grading'
down_revision = '0004_add_email_selection'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('leads', sa.Column('entity_kind', sa.VARCHAR(255), nullable=True))
    op.add_column('leads', sa.Column('phone', sa.VARCHAR(255), nullable=True))
    op.add_column('leads', sa.Column('channel_tag', sa.VARCHAR(255), nullable=True))
    op.add_column('leads', sa.Column('confidence_band', sa.VARCHAR(255), nullable=True))
    op.add_column('leads', sa.Column('field_provenance', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('leads', 'field_provenance')
    op.drop_column('leads', 'confidence_band')
    op.drop_column('leads', 'channel_tag')
    op.drop_column('leads', 'phone')
    op.drop_column('leads', 'entity_kind')
