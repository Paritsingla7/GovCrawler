"""Add campaign_credentials junction table, credential_id audit columns, daily_send_limit

Revision ID: 0008_add_campaign_credentials
Revises: 0007_add_domain_external_id
"""
from alembic import op
import sqlalchemy as sa

revision = '0008_add_campaign_credentials'
down_revision = '0007_add_domain_external_id'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'campaign_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('campaign_id', sa.Integer(), sa.ForeignKey('campaigns.id'), nullable=False),
        sa.Column('credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id'), nullable=False),
        sa.UniqueConstraint('campaign_id', 'credential_id', name='uq_campaign_credential'),
    )
    op.create_index('ix_campaign_credentials_campaign_id', 'campaign_credentials', ['campaign_id'])

    op.add_column('smtp_credentials', sa.Column('daily_send_limit', sa.Integer(), nullable=True))
    op.add_column('campaign_emails', sa.Column('credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id'), nullable=True))
    op.add_column('test_campaign_emails', sa.Column('credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id'), nullable=True))


def downgrade():
    op.drop_column('test_campaign_emails', 'credential_id')
    op.drop_column('campaign_emails', 'credential_id')
    op.drop_column('smtp_credentials', 'daily_send_limit')

    op.drop_index('ix_campaign_credentials_campaign_id', table_name='campaign_credentials')
    op.drop_table('campaign_credentials')
