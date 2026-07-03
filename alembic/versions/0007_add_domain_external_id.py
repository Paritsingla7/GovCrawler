"""Add external_id to domains for null-url dedup

Revision ID: 0007_add_domain_external_id
Revises: 0006_add_job_custom_urls
"""
from alembic import op
import sqlalchemy as sa

revision = '0007_add_domain_external_id'
down_revision = '0006_add_job_custom_urls'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('domains', sa.Column('external_id', sa.String(), nullable=True))
    op.create_index('ix_domains_external_id', 'domains', ['external_id'])


def downgrade():
    op.drop_index('ix_domains_external_id', table_name='domains')
    op.drop_column('domains', 'external_id')
