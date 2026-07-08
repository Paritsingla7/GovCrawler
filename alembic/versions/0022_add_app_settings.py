"""Add app_settings table (DB-backed crawl policy)

Revision ID: 0022_add_app_settings
Revises: 0021_add_job_frontier

Phase 8, plan.md §19.1 / §3.2. Schema-only — the `crawl_policy` row is
seeded by Database._seed_app_settings() at app startup (matching how
seed_rbac()/_recompute_lead_scores() already do data seeding outside
Alembic in this codebase), not here. Plain sa.JSON (not postgresql.JSONB)
so the column works unchanged on the SQLite desktop default too.
"""
import sqlalchemy as sa

from alembic import op

revision = '0022_add_app_settings'
down_revision = '0021_add_job_frontier'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'app_settings' in inspector.get_table_names():
        return
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'app_settings' not in inspector.get_table_names():
        return
    op.drop_table('app_settings')
