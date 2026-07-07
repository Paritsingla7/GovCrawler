"""Add auth/RBAC tables (users, roles, permissions, sessions, audit_log)

Revision ID: 0012_add_auth
Revises: 0011_add_crawl_snapshots

Phase 0 — auth foundation. New tables only (no ALTERs of existing tables),
so this is safe on both SQLite (Database.__init__ already creates these via
create_all() before this migration runs) and Postgres. Guarded with an
inspector per the 0011 precedent.
"""
import sqlalchemy as sa

from alembic import op

revision = '0012_add_auth'
down_revision = '0011_add_crawl_snapshots'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'roles' not in tables:
        op.create_table(
            'roles',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('name', sa.String(), nullable=False, unique=True),
            sa.Column('description', sa.String(), nullable=True),
            sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if 'permissions' not in tables:
        op.create_table(
            'permissions',
            sa.Column('key', sa.String(), primary_key=True),
            sa.Column('description', sa.String(), nullable=False),
        )

    if 'users' not in tables:
        op.create_table(
            'users',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('email', sa.String(), nullable=False, unique=True),
            sa.Column('password_hash', sa.String(), nullable=False),
            sa.Column('full_name', sa.String(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('role_id', sa.Integer(), sa.ForeignKey('roles.id'), nullable=True),
            sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('failed_logins', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('locked_until', sa.DateTime(), nullable=True),
            sa.Column('last_login_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_users_email', 'users', ['email'])

    if 'role_permissions' not in tables:
        op.create_table(
            'role_permissions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('role_id', sa.Integer(), sa.ForeignKey('roles.id'), nullable=False),
            sa.Column('permission_key', sa.String(), sa.ForeignKey('permissions.key'), nullable=False),
            sa.UniqueConstraint('role_id', 'permission_key', name='uq_role_permission'),
        )
        op.create_index('ix_role_permissions_role_id', 'role_permissions', ['role_id'])

    if 'user_permissions' not in tables:
        op.create_table(
            'user_permissions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('permission_key', sa.String(), sa.ForeignKey('permissions.key'), nullable=False),
            sa.Column('effect', sa.String(), nullable=False),
            sa.UniqueConstraint('user_id', 'permission_key', name='uq_user_permission'),
        )
        op.create_index('ix_user_permissions_user_id', 'user_permissions', ['user_id'])

    if 'user_sessions' not in tables:
        op.create_table(
            'user_sessions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('refresh_token_hash', sa.String(), nullable=False),
            sa.Column('user_agent', sa.String(), nullable=True),
            sa.Column('ip', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_user_sessions_user_id', 'user_sessions', ['user_id'])
        op.create_index('ix_user_sessions_refresh_token_hash', 'user_sessions', ['refresh_token_hash'])

    if 'audit_log' not in tables:
        op.create_table(
            'audit_log',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('action', sa.String(), nullable=False),
            sa.Column('target_type', sa.String(), nullable=True),
            sa.Column('target_id', sa.String(), nullable=True),
            sa.Column('detail', sa.Text(), nullable=True),
            sa.Column('ip', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_audit_log_created_at', 'audit_log', ['created_at'])


def downgrade():
    op.drop_index('ix_audit_log_created_at', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('ix_user_sessions_refresh_token_hash', table_name='user_sessions')
    op.drop_index('ix_user_sessions_user_id', table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_index('ix_user_permissions_user_id', table_name='user_permissions')
    op.drop_table('user_permissions')
    op.drop_index('ix_role_permissions_role_id', table_name='role_permissions')
    op.drop_table('role_permissions')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
    op.drop_table('permissions')
    op.drop_table('roles')
