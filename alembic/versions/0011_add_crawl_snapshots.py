"""Add crawl_snapshots table

Revision ID: 0011_add_crawl_snapshots
Revises: 0010_add_lead_score

Decouples leads (and a job's seed view) from the mutable `domains` catalog:
each crawl freezes its seed domains' metadata into `crawl_snapshots`, and leads
point at the snapshot instead of `domains`. This makes lead-visible metadata
immune to the destructive catalog rebuild that happens on every import/refresh.

The `leads.snapshot_id` column and the one-time backfill are deliberately NOT
done here. Confirmed via `alembic check` that this project's SQLite databases
already carry pre-existing drift where an ALTER-added foreign-key column
(`campaign_emails.credential_id`, `test_campaign_emails.credential_id`) never
actually took effect through Alembic despite the migration being marked
applied — those columns exist today only because `Database._ensure_columns()`
(a bare, non-FK `ALTER TABLE ADD COLUMN`) added them. `leads.snapshot_id` hit
the identical failure mode. So, consistent with `0010_add_lead_score`'s own
precedent, the column + backfill live in `_ensure_columns()` /
`Database._backfill_snapshots()` instead — the actually-proven schema-evolution
path for altering an existing table in this app.

Guarded with an inspector because `Database.__init__` runs `create_all()`
before `run_migrations()`, so `crawl_snapshots` may already exist by the time
this migration runs.
"""
import sqlalchemy as sa

from alembic import op

revision = '0011_add_crawl_snapshots'
down_revision = '0010_add_lead_score'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'crawl_snapshots' not in tables:
        op.create_table(
            'crawl_snapshots',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('job_id', sa.Integer(), sa.ForeignKey('crawl_jobs.id'), nullable=False),
            sa.Column('source_domain_id', sa.Integer(), nullable=True),
            sa.Column('external_id', sa.String(), nullable=True),
            sa.Column('category_code', sa.String(), nullable=True),
            sa.Column('category_title', sa.String(), nullable=True),
            sa.Column('state', sa.String(), nullable=True),
            sa.Column('org_type', sa.String(), nullable=True),
            sa.Column('org_type_title', sa.String(), nullable=True),
            sa.Column('title', sa.String(), nullable=True),
            sa.Column('main_url', sa.String(), nullable=True),
            sa.Column('contact_url', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('job_id', 'source_domain_id', name='uq_snapshot_job_domain'),
        )
        op.create_index('ix_crawl_snapshots_job_id', 'crawl_snapshots', ['job_id'])


def downgrade():
    op.drop_index('ix_crawl_snapshots_job_id', table_name='crawl_snapshots')
    op.drop_table('crawl_snapshots')
