"""Add lead_occurrences + manual_state; drop domain_id/domain_state/domain_org_type

Revision ID: 0015_lead_occ_manual_state
Revises: 0014_add_ownership

Phase 2, chunk 2 (lead enrich-dedup). `save_lead()`/`bulk_upsert_manual_leads()`
move from insert-only-reject-on-duplicate-email to enrich-on-conflict, and
every capture of a shared lead is now recorded in `lead_occurrences` instead
of being silently dropped after the first one. `leads.domain_state` (today
editable on ANY lead, crawled or manual) is replaced by `manual_state`,
editable only on snapshot-less (manual/CSV) leads — crawled leads now read
their state exclusively from the `crawl_snapshots` join at display time (see
lead_mixin.py). `domain_id`/`domain_org_type` are dropped outright (vestigial
— see plan.md §4.5). Guarded with an inspector per the 0011-0014 precedent.

Note: revision id kept to <=32 chars — alembic_version.version_num is
VARCHAR(32) (Alembic's own default), and the original longer id
('0015_lead_occurrences_and_manual_state') overflowed it.
"""
import logging

import sqlalchemy as sa

from alembic import op

log = logging.getLogger(__name__)

revision = '0015_lead_occ_manual_state'
down_revision = '0014_add_ownership'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'lead_occurrences' not in tables:
        op.create_table(
            'lead_occurrences',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('lead_id', sa.Integer(), sa.ForeignKey('leads.id', ondelete='CASCADE'), nullable=False),
            sa.Column('job_id', sa.Integer(), sa.ForeignKey('crawl_jobs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('captured_by', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('source_url', sa.String()),
            sa.Column('captured_at', sa.DateTime()),
            sa.UniqueConstraint('lead_id', 'job_id', name='uq_lead_occurrence_lead_job'),
        )
        op.create_index('ix_lead_occurrences_lead_id', 'lead_occurrences', ['lead_id'])
        op.create_index('ix_lead_occurrences_job_id', 'lead_occurrences', ['job_id'])

    if 'leads' not in tables:
        return

    lead_columns = {c['name'] for c in inspector.get_columns('leads')}

    if 'manual_state' not in lead_columns:
        with op.batch_alter_table('leads') as batch_op:
            batch_op.add_column(sa.Column('manual_state', sa.String(), nullable=True))
        lead_columns.add('manual_state')

    if 'domain_state' in lead_columns:
        # Manual/CSV leads (no snapshot_id) carry their only state signal on
        # domain_state today; copy it into manual_state before the column is
        # dropped. Crawled leads' domain_state is superseded by the snapshot
        # join and is discarded (see confirmed decision in plan.md).
        #
        # `snapshot_id` itself is never Alembic-managed (see 0011's docstring
        # — it's only ever added by Database._ensure_columns() at app
        # startup), so it may not exist yet here: a standalone `alembic
        # upgrade head` against a brand-new DB (e.g. deploy/docker-compose's
        # `migrate` service, which never constructs a `Database()`) hits this
        # migration before that column is ever created. Without it there's no
        # snapshot linkage to filter on at all, so every domain_state carrier
        # is copied unconditionally.
        if 'snapshot_id' in lead_columns:
            bind.execute(sa.text(
                "UPDATE leads SET manual_state = domain_state "
                "WHERE snapshot_id IS NULL AND domain_state IS NOT NULL AND manual_state IS NULL"
            ))
        else:
            bind.execute(sa.text(
                "UPDATE leads SET manual_state = domain_state "
                "WHERE domain_state IS NOT NULL AND manual_state IS NULL"
            ))

    # Backfill one lead_occurrences row per existing lead (no attribution data
    # exists pre-migration, so captured_by is left NULL).
    bind.execute(sa.text(
        "INSERT INTO lead_occurrences (lead_id, job_id, source_url, captured_at) "
        "SELECT l.id, l.job_id, l.source_url, l.captured_at FROM leads l "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM lead_occurrences o WHERE o.lead_id = l.id AND o.job_id = l.job_id"
        ")"
    ))

    # naming_convention: dropping a column can force a full table recreate on
    # SQLite, and `leads` already has unnamed FKs (job_id, snapshot_id) from
    # earlier migrations — without this, recreate hits the same "Constraint
    # must have a name" error 0014 did for a freshly-added FK, just for a
    # pre-existing one instead. See 0014/0016 for the same fix.
    with op.batch_alter_table(
        'leads',
        naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"},
    ) as batch_op:
        if 'domain_id' in lead_columns:
            batch_op.drop_column('domain_id')
        if 'domain_state' in lead_columns:
            batch_op.drop_column('domain_state')
        if 'domain_org_type' in lead_columns:
            batch_op.drop_column('domain_org_type')


def downgrade():
    with op.batch_alter_table('leads') as batch_op:
        batch_op.add_column(sa.Column('domain_id', sa.Integer()))
        batch_op.add_column(sa.Column('domain_state', sa.String()))
        batch_op.add_column(sa.Column('domain_org_type', sa.String()))
        batch_op.drop_column('manual_state')
    op.drop_table('lead_occurrences')
