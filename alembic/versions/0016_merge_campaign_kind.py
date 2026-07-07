"""Merge test_campaigns/test_campaign_emails into campaigns/campaign_emails via kind

Revision ID: 0016_merge_campaign_kind
Revises: 0015_lead_occ_manual_state

Phase 2, chunk 3. `campaigns.kind` ('production'/'test', TEXT+CHECK-style —
not a native PG enum, per plan.md §4's portability rationale) replaces the
separate test_campaigns/test_campaign_emails table pair. `test_credential_id`
moves onto `campaigns` (meaningful only for kind='test'); `campaign_emails
.lead_id` becomes nullable (NULL for merged-in test/dummy rows). Data is
copied by natural key (name + created_at) rather than assuming a fresh ID
space, matching the migrate_sqlite_to_pg.py precedent from Phase 1, then the
two old tables are dropped. Guarded with an inspector per the 0011-0015
precedent — safe to re-run if partially applied.
"""
import logging

import sqlalchemy as sa

from alembic import op

log = logging.getLogger(__name__)

revision = '0016_merge_campaign_kind'
down_revision = '0015_lead_occ_manual_state'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'campaigns' not in tables:
        return

    campaign_columns = {c['name'] for c in inspector.get_columns('campaigns')}
    if 'kind' not in campaign_columns:
        with op.batch_alter_table('campaigns') as batch_op:
            batch_op.add_column(sa.Column('kind', sa.String(), nullable=False, server_default='production'))
    if 'test_credential_id' not in campaign_columns:
        with op.batch_alter_table('campaigns') as batch_op:
            batch_op.add_column(sa.Column('test_credential_id', sa.Integer(),
                                          sa.ForeignKey('smtp_credentials.id')))

    if 'campaign_emails' in tables:
        email_columns = {c['name'] for c in inspector.get_columns('campaign_emails')}
        lead_id_col = next((c for c in inspector.get_columns('campaign_emails') if c['name'] == 'lead_id'), None)
        if lead_id_col is not None and not lead_id_col.get('nullable', True):
            with op.batch_alter_table('campaign_emails') as batch_op:
                batch_op.alter_column('lead_id', nullable=True)

    if 'test_campaigns' not in tables:
        return

    # Copy each test_campaigns row into campaigns (kind='test') if not already
    # migrated, matched by natural key (name, created_at) so this is safe to
    # re-run on a partially-applied prior attempt.
    old_campaigns = bind.execute(sa.text(
        "SELECT id, name, template_id, test_credential_id, status, pause_reason, "
        "owner_id, created_at FROM test_campaigns"
    )).fetchall()

    id_map = {}  # old test_campaigns.id -> new campaigns.id
    for row in old_campaigns:
        existing = bind.execute(sa.text(
            "SELECT id FROM campaigns WHERE name = :name AND created_at = :created_at AND kind = 'test'"
        ), {"name": row.name, "created_at": row.created_at}).first()
        if existing:
            id_map[row.id] = existing[0]
            continue
        bind.execute(sa.text(
            "INSERT INTO campaigns (name, template_id, kind, test_credential_id, status, "
            "pause_reason, owner_id, created_at) "
            "VALUES (:name, :template_id, 'test', :test_credential_id, :status, "
            ":pause_reason, :owner_id, :created_at)"
        ), {
            "name": row.name, "template_id": row.template_id,
            "test_credential_id": row.test_credential_id, "status": row.status,
            "pause_reason": row.pause_reason, "owner_id": row.owner_id,
            "created_at": row.created_at,
        })
        # Portable across SQLite/Postgres — psycopg2's CursorResult doesn't
        # populate lastrowid, so re-select by the same natural key instead.
        new_id = bind.execute(
            sa.text("SELECT id FROM campaigns WHERE name = :name AND created_at = :created_at AND kind = 'test'"),
            {"name": row.name, "created_at": row.created_at},
        ).first()[0]
        id_map[row.id] = new_id

    if 'test_campaign_emails' in tables and id_map:
        old_emails = bind.execute(sa.text(
            "SELECT test_campaign_id, recipient_email, subject, body, status, is_selected, "
            "missing_fields, error_message, credential_id, sent_at FROM test_campaign_emails"
        )).fetchall()
        for row in old_emails:
            new_campaign_id = id_map.get(row.test_campaign_id)
            if new_campaign_id is None:
                continue
            already = bind.execute(sa.text(
                "SELECT 1 FROM campaign_emails WHERE campaign_id = :cid AND recipient_email = :email "
                "AND lead_id IS NULL"
            ), {"cid": new_campaign_id, "email": row.recipient_email}).first()
            if already:
                continue
            bind.execute(sa.text(
                "INSERT INTO campaign_emails (campaign_id, lead_id, recipient_email, subject, body, "
                "status, is_selected, missing_fields, error_message, credential_id, sent_at) "
                "VALUES (:cid, NULL, :email, :subject, :body, :status, :is_selected, "
                ":missing_fields, :error_message, :credential_id, :sent_at)"
            ), {
                "cid": new_campaign_id, "email": row.recipient_email, "subject": row.subject,
                "body": row.body, "status": row.status, "is_selected": row.is_selected,
                "missing_fields": row.missing_fields, "error_message": row.error_message,
                "credential_id": row.credential_id, "sent_at": row.sent_at,
            })

    log.info(f"0016_merge_campaign_kind: migrated {len(id_map)} test campaign(s)")

    if 'test_campaign_emails' in tables:
        op.drop_table('test_campaign_emails')
    op.drop_table('test_campaigns')


def downgrade():
    op.create_table(
        'test_campaigns',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('email_templates.id')),
        sa.Column('test_credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id')),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('created_at', sa.DateTime()),
        sa.Column('pause_reason', sa.String()),
    )
    op.create_table(
        'test_campaign_emails',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('test_campaign_id', sa.Integer(), sa.ForeignKey('test_campaigns.id'), nullable=False),
        sa.Column('recipient_email', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('is_selected', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('missing_fields', sa.String()),
        sa.Column('error_message', sa.String()),
        sa.Column('sent_at', sa.DateTime()),
        sa.Column('credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id')),
    )
    with op.batch_alter_table('campaign_emails') as batch_op:
        batch_op.alter_column('lead_id', nullable=False)
    with op.batch_alter_table('campaigns') as batch_op:
        batch_op.drop_column('test_credential_id')
        batch_op.drop_column('kind')
