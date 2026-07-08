"""Add crawl_jobs.current_depth/active_workers and leads.snapshot_id via Alembic

Revision ID: 0024_ddl_owned_ensure_columns
Revises: 0023_drop_visited_and_frontier

Phase 5's least-privilege `govcrawler_app` Postgres role (0020) revoked DDL
rights from the runtime connection api/dispatcher use — but these three
columns were, per 0000/0011's own docstrings, deliberately kept OUT of the
Alembic chain and left to `Database._ensure_columns()`'s raw `ALTER TABLE`
at app startup instead (worked around a SQLite quirk where an Alembic
ALTER-added FK column could silently not take effect despite the migration
being marked applied). That reasoning doesn't hold for Postgres, and now
directly conflicts with 0020: `_ensure_columns()` runs inside
`Database.__init__()` using whichever connection role the caller has, which
for api/dispatcher is the DDL-less `govcrawler_app` role — so on any
Postgres deploy where migrations run standalone via the `migrate` service
(deploy/docker-compose.yml), these three columns are never added and every
subsequent api/dispatcher startup fails with `InsufficientPrivilege`.

Every other column `_ensure_columns()` lists is already covered by an
earlier Alembic migration (0004/0005/0007/0008/0009/0010/0016/0019) — its
duplicate checks there are a defensive no-op for old SQLite installs with
that same drift, not a second source of truth. `_ensure_columns()` itself is
untouched and keeps serving desktop SQLite installs, where there is no
role split and this conflict doesn't exist.

Guarded with inspector checks (0011-0016 precedent) so this is a no-op on a
DB where `_ensure_columns()` already added these columns through the old
in-process path (e.g. an existing desktop SQLite install, or a Postgres
instance bootstrapped by hand under a superuser role before this migration
existed).
"""
import sqlalchemy as sa

from alembic import op

revision = '0024_ddl_owned_ensure_columns'
down_revision = '0023_drop_visited_and_frontier'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'crawl_jobs' in tables:
        job_columns = {c['name'] for c in inspector.get_columns('crawl_jobs')}
        with op.batch_alter_table('crawl_jobs') as batch_op:
            if 'current_depth' not in job_columns:
                batch_op.add_column(sa.Column('current_depth', sa.Integer(), nullable=False, server_default='0'))
            if 'active_workers' not in job_columns:
                batch_op.add_column(sa.Column('active_workers', sa.Integer(), nullable=False, server_default='0'))

    if 'leads' in tables:
        lead_columns = {c['name'] for c in inspector.get_columns('leads')}
        if 'snapshot_id' not in lead_columns:
            with op.batch_alter_table('leads') as batch_op:
                batch_op.add_column(sa.Column('snapshot_id', sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'leads' in tables:
        lead_columns = {c['name'] for c in inspector.get_columns('leads')}
        if 'snapshot_id' in lead_columns:
            with op.batch_alter_table('leads') as batch_op:
                batch_op.drop_column('snapshot_id')

    if 'crawl_jobs' in tables:
        job_columns = {c['name'] for c in inspector.get_columns('crawl_jobs')}
        with op.batch_alter_table('crawl_jobs') as batch_op:
            if 'active_workers' in job_columns:
                batch_op.drop_column('active_workers')
            if 'current_depth' in job_columns:
                batch_op.drop_column('current_depth')
