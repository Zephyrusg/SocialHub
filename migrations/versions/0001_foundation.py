"""Initial platform-neutral schema; UTC DATETIME values."""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None


def upgrade():
    op.create_table('accounts', sa.Column('id', sa.String(64), primary_key=True), sa.Column('platform', sa.String(32), nullable=False), sa.Column('remote_id', sa.String(128), nullable=False), sa.Column('name', sa.String(128), nullable=False), sa.Column('account_type', sa.String(32), nullable=False), sa.Column('allowlisted', sa.Boolean(), nullable=False), sa.Column('is_mock', sa.Boolean(), nullable=False), sa.UniqueConstraint('platform', 'remote_id'))
    op.create_table('capabilities', sa.Column('account_id', sa.String(64), sa.ForeignKey('accounts.id'), primary_key=True), sa.Column('operation', sa.String(64), primary_key=True), sa.Column('supported', sa.Boolean(), nullable=False), sa.Column('details', sa.JSON(), nullable=False))
    op.create_table('credentials', sa.Column('id', sa.String(36), primary_key=True), sa.Column('account_id', sa.String(64), sa.ForeignKey('accounts.id'), unique=True, nullable=False), sa.Column('ciphertext', sa.Text(), nullable=False), sa.Column('scopes', sa.JSON(), nullable=False), sa.Column('expires_at', sa.DateTime()), sa.Column('refreshed_at', sa.DateTime()), sa.Column('revoked', sa.Boolean(), nullable=False))
    op.create_table('posts', sa.Column('id', sa.String(36), primary_key=True), sa.Column('account_id', sa.String(64), sa.ForeignKey('accounts.id'), nullable=False), sa.Column('remote_id', sa.String(128)), sa.Column('content', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.UniqueConstraint('account_id', 'remote_id'))
    op.create_table('media', sa.Column('id', sa.String(36), primary_key=True), sa.Column('sha256', sa.String(64), unique=True, nullable=False), sa.Column('path', sa.Text(), nullable=False), sa.Column('mime_type', sa.String(80), nullable=False), sa.Column('size', sa.Integer(), nullable=False), sa.Column('metadata_json', sa.JSON(), nullable=False), sa.Column('expires_at', sa.DateTime()))
    op.create_table('jobs', sa.Column('id', sa.String(36), primary_key=True), sa.Column('account_id', sa.String(64), sa.ForeignKey('accounts.id'), nullable=False), sa.Column('run_at', sa.DateTime(), nullable=False), sa.Column('requested_timezone', sa.String(64), nullable=False), sa.Column('payload', sa.JSON(), nullable=False), sa.Column('state', sa.String(32), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('next_attempt_at', sa.DateTime()), sa.Column('authorization_subject', sa.String(128), nullable=False), sa.Column('idempotency_key', sa.String(128), unique=True, nullable=False))
    op.create_index('ix_jobs_run_at', 'jobs', ['run_at'])
    op.create_table('audit_logs', sa.Column('id', sa.String(36), primary_key=True), sa.Column('at', sa.DateTime(), nullable=False), sa.Column('subject', sa.String(128), nullable=False), sa.Column('operation', sa.String(64), nullable=False), sa.Column('account_id', sa.String(64)), sa.Column('outcome', sa.String(32), nullable=False), sa.Column('details', sa.JSON(), nullable=False))
    op.create_table('idempotency_records', sa.Column('key', sa.String(128), primary_key=True), sa.Column('subject', sa.String(128), nullable=False), sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('response', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('operations', sa.Column('id', sa.String(36), primary_key=True), sa.Column('account_id', sa.String(64)), sa.Column('operation', sa.String(64), nullable=False), sa.Column('status', sa.String(32), nullable=False), sa.Column('error_code', sa.String(64)), sa.Column('retryable', sa.Boolean(), nullable=False), sa.Column('at', sa.DateTime(), nullable=False))


def downgrade():
    for name in ['operations', 'idempotency_records', 'audit_logs', 'jobs', 'media', 'posts', 'credentials', 'capabilities', 'accounts']:
        op.drop_table(name)
