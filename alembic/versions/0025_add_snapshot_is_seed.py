"""Add crawl_snapshots.is_seed

Revision ID: 0025_add_snapshot_is_seed
Revises: 0024_ddl_owned_ensure_columns

Lead domain attribution (PLAN_attribution_and_parser.md Plan 1, WI-4) will
start minting crawl_snapshots rows for domains a crawl *discovered* by
following links, not just the ones a user selected as seeds. Without this
flag, GET /api/jobs/{id}/seeds (backed by get_crawl_snapshots) would start
showing those discovered domains in the "Job Seeds" UI alongside the real
seeds. Existing rows default True — every snapshot created before this
migration was, in fact, a seed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0025_add_snapshot_is_seed"
down_revision = "0024_ddl_owned_ensure_columns"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_snapshots" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("crawl_snapshots")}
    if "is_seed" not in columns:
        with op.batch_alter_table("crawl_snapshots") as batch_op:
            batch_op.add_column(sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crawl_snapshots" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("crawl_snapshots")}
    if "is_seed" in columns:
        with op.batch_alter_table("crawl_snapshots") as batch_op:
            batch_op.drop_column("is_seed")
