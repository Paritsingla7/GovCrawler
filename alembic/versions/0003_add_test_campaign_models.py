"""Add test campaign models

Revision ID: 0003_add_test_campaign_models
Revises: 0002_patch_campaign_fields
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0003_add_test_campaign_models'
down_revision = '0002_patch_campaign_fields'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'test_campaigns',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('email_templates.id'), nullable=True),
        sa.Column('test_credential_id', sa.Integer(), sa.ForeignKey('smtp_credentials.id'), nullable=True),
        sa.Column('status', sa.Enum('RUNNING', 'PAUSED', 'CANCELLED', 'COMPLETED', name='campaignstatus'),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'test_campaign_emails',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('test_campaign_id', sa.Integer(), sa.ForeignKey('test_campaigns.id'), nullable=False),
        sa.Column('recipient_email', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.Enum('DRAFT', 'QUEUED', 'SENT', 'FAILED', name='emailstatus'), nullable=False,
                  server_default='DRAFT'),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('test_campaign_emails')
    op.drop_table('test_campaigns')
