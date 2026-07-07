"""Encrypt smtp_credentials.password at rest

Revision ID: 0017_encrypt_credentials
Revises: 0016_merge_campaign_kind

Phase 2, chunk 4 (credential encryption). `password` (plaintext) is renamed to
`password_encrypted` and every existing plaintext value is re-encrypted in
place with the same env-first-else-persisted-file key resolution
(`ensure_credential_enc_key`, `portal/security/crypto.py`) the app uses at
runtime, so a credential written before this migration decrypts correctly
afterward. Losing CREDENTIAL_ENC_KEY makes existing credentials permanently
undecryptable — there is no recovery path, by design (plan.md §13). Guarded
with an inspector per the 0011-0016 precedent.
"""
import logging
import os
import sys

import sqlalchemy as sa
import yaml

from alembic import op

log = logging.getLogger(__name__)

revision = '0017_encrypt_credentials'
down_revision = '0016_merge_campaign_kind'
branch_labels = None
depends_on = None

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


def _resolve_key():
    from portal.paths import LIVE_CONFIG_PATH
    from portal.security.crypto import ensure_credential_enc_key

    config_dict = {}
    if LIVE_CONFIG_PATH.exists():
        with open(LIVE_CONFIG_PATH) as f:
            config_dict = yaml.safe_load(f) or {}
    return ensure_credential_enc_key(config_dict, LIVE_CONFIG_PATH)


def upgrade():
    from portal.security.crypto import encrypt_password

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'smtp_credentials' not in inspector.get_table_names():
        return

    columns = {c['name'] for c in inspector.get_columns('smtp_credentials')}
    if 'password_encrypted' in columns:
        return  # already migrated

    with op.batch_alter_table('smtp_credentials') as batch_op:
        batch_op.add_column(sa.Column('password_encrypted', sa.LargeBinary(), nullable=True))

    key = _resolve_key()
    rows = bind.execute(sa.text("SELECT id, password FROM smtp_credentials")).fetchall()
    for row in rows:
        if row.password is None:
            continue
        bind.execute(
            sa.text("UPDATE smtp_credentials SET password_encrypted = :enc WHERE id = :id"),
            {"enc": encrypt_password(row.password, key), "id": row.id},
        )
    log.info(f"0017_encrypt_credentials: encrypted {len(rows)} existing credential(s)")

    with op.batch_alter_table('smtp_credentials') as batch_op:
        batch_op.drop_column('password')
        batch_op.alter_column('password_encrypted', nullable=False)


def downgrade():
    from portal.security.crypto import decrypt_password

    bind = op.get_bind()
    with op.batch_alter_table('smtp_credentials') as batch_op:
        batch_op.add_column(sa.Column('password', sa.String(), nullable=True))

    key = _resolve_key()
    rows = bind.execute(sa.text("SELECT id, password_encrypted FROM smtp_credentials")).fetchall()
    for row in rows:
        if row.password_encrypted is None:
            continue
        bind.execute(
            sa.text("UPDATE smtp_credentials SET password = :plain WHERE id = :id"),
            {"plain": decrypt_password(row.password_encrypted, key), "id": row.id},
        )

    with op.batch_alter_table('smtp_credentials') as batch_op:
        batch_op.drop_column('password_encrypted')
        batch_op.alter_column('password', nullable=False)
