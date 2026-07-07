"""Add job_frontiers table (optional cross-machine resume)

Revision ID: 0021_add_job_frontier
Revises: 0020_least_privilege_role

Phase 6, plan.md §18 Decision C (off by default via crawler.cross_machine_resume).
Local per-job SQLite (agent/local_store.py) remains the primary frontier
checkpoint; this table lets a different machine than the one that ran the
original crawl fetch a snapshot on resume, at the cost of extra write volume
when the flag is enabled. Plain Text/DateTime columns — no enum branching
needed, unlike 0019/0020.
"""
import sqlalchemy as sa

from alembic import op

revision = '0021_add_job_frontier'
down_revision = '0020_least_privilege_role'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'job_frontiers' in inspector.get_table_names():
        return
    op.create_table(
        'job_frontiers',
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('crawl_jobs.id'), primary_key=True),
        sa.Column('snapshot_json', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'job_frontiers' not in inspector.get_table_names():
        return
    op.drop_table('job_frontiers')
