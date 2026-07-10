"""Cascade crawl_job_domains.domain_id on domain delete

Revision ID: 0027_cascade_job_domains_fk
Revises: 0026_smtp_oauth

crawl_job_domains.domain_id had a plain FK to domains.id with no ondelete
rule, so Postgres defaults to RESTRICT — a catalog re-import's DELETE FROM
domains fails outright (IntegrityError) the moment any crawl job has ever
been created from a catalog domain. Leads/snapshots never point at domains
directly (they freeze into crawl_snapshots), so losing this junction row on
a re-import is safe — CASCADE just drops the now-stale link.
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_cascade_job_domains_fk"
down_revision = "0026_smtp_oauth"
branch_labels = None
depends_on = None

_TABLE = "crawl_job_domains"
_COLUMN = "domain_id"
_REF_TABLE = "domains"


def _domain_id_fk_name(inspector) -> str | None:
    for fk in inspector.get_foreign_keys(_TABLE):
        if fk.get("referred_table") == _REF_TABLE and fk.get("constrained_columns") == [_COLUMN]:
            return fk.get("name")
    return None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    for fk in inspector.get_foreign_keys(_TABLE):
        if fk.get("referred_table") == _REF_TABLE and fk.get("constrained_columns") == [_COLUMN]:
            if fk.get("options", {}).get("ondelete") == "CASCADE":
                return  # already cascading
            break

    fk_name = _domain_id_fk_name(inspector)
    with op.batch_alter_table(_TABLE) as batch_op:
        if fk_name:
            batch_op.drop_constraint(fk_name, type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_crawl_job_domains_domain_id", _REF_TABLE, [_COLUMN], ["id"], ondelete="CASCADE"
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    fk_name = _domain_id_fk_name(inspector)
    with op.batch_alter_table(_TABLE) as batch_op:
        if fk_name:
            batch_op.drop_constraint(fk_name, type_="foreignkey")
        batch_op.create_foreign_key("fk_crawl_job_domains_domain_id", _REF_TABLE, [_COLUMN], ["id"])
