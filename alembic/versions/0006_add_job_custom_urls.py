"""Add source_type to crawl_jobs and job_custom_urls table

Revision ID: 0006_add_job_custom_urls
Revises: 0005_add_lead_depth, 0005_add_lead_grading
"""
from alembic import op
import sqlalchemy as sa

revision = '0006_add_job_custom_urls'
down_revision = ('0005_add_lead_depth', '0005_add_lead_grading')
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'crawl_jobs',
        sa.Column('source_type', sa.String(), nullable=False, server_default='domains')
    )
    op.create_table(
        'job_custom_urls',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('crawl_jobs.id'), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('job_id', 'url', name='uq_job_custom_url'),
    )
    op.create_index('ix_job_custom_urls_job_id', 'job_custom_urls', ['job_id'])


def downgrade():
    op.drop_index('ix_job_custom_urls_job_id', table_name='job_custom_urls')
    op.drop_table('job_custom_urls')
    op.drop_column('crawl_jobs', 'source_type')
