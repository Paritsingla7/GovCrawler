"""Add OAuth2 (XOAUTH2) fields to smtp_credentials + oauth_pending_flows

Revision ID: 0025_smtp_oauth
Revises: 0024_ddl_owned_ensure_columns

Microsoft/Exchange Online has dropped SMTP basic auth; sending through those
mailboxes now requires OAuth2 (Authorization Code + PKCE, XOAUTH2 at the SMTP
layer). Adds `provider` (default 'basic', unchanged behavior for every
existing row) plus encrypted refresh/access token columns to
`smtp_credentials`, makes `password_encrypted` nullable (OAuth rows never
populate it), and adds `oauth_pending_flows`, the short-lived state table that
bridges an authorize-redirect round trip to Microsoft/Google back to the
credential it's connecting. See .docs/outreach.md.

Guarded with inspector checks per the 0011-0016 / 0024 precedent.
"""
import sqlalchemy as sa

from alembic import op

revision = '0025_smtp_oauth'
down_revision = '0024_ddl_owned_ensure_columns'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'smtp_credentials' in tables:
        columns = {c['name'] for c in inspector.get_columns('smtp_credentials')}
        with op.batch_alter_table('smtp_credentials') as batch_op:
            if 'provider' not in columns:
                batch_op.add_column(
                    sa.Column('provider', sa.String(), nullable=False, server_default='basic')
                )
            if 'refresh_token_encrypted' not in columns:
                batch_op.add_column(sa.Column('refresh_token_encrypted', sa.LargeBinary(), nullable=True))
            if 'access_token_encrypted' not in columns:
                batch_op.add_column(sa.Column('access_token_encrypted', sa.LargeBinary(), nullable=True))
            if 'token_expires_at' not in columns:
                batch_op.add_column(sa.Column('token_expires_at', sa.DateTime(), nullable=True))
            if 'password_encrypted' in columns:
                batch_op.alter_column('password_encrypted', existing_type=sa.LargeBinary(), nullable=True)

    if 'oauth_pending_flows' not in tables:
        op.create_table(
            'oauth_pending_flows',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('state', sa.String(), nullable=False),
            sa.Column('credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id'), nullable=False),
            sa.Column('provider', sa.String(), nullable=False),
            sa.Column('code_verifier', sa.String(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_oauth_pending_flows_state', 'oauth_pending_flows', ['state'], unique=True)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'oauth_pending_flows' in tables:
        op.drop_table('oauth_pending_flows')

    if 'smtp_credentials' in tables:
        columns = {c['name'] for c in inspector.get_columns('smtp_credentials')}
        with op.batch_alter_table('smtp_credentials') as batch_op:
            if 'password_encrypted' in columns:
                batch_op.alter_column('password_encrypted', existing_type=sa.LargeBinary(), nullable=False)
            if 'token_expires_at' in columns:
                batch_op.drop_column('token_expires_at')
            if 'access_token_encrypted' in columns:
                batch_op.drop_column('access_token_encrypted')
            if 'refresh_token_encrypted' in columns:
                batch_op.drop_column('refresh_token_encrypted')
            if 'provider' in columns:
                batch_op.drop_column('provider')
