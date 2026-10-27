"""Immutable previews, independent target status, owner approvals and media references."""
from alembic import op
import sqlalchemy as sa
revision = '0003'
down_revision = '0002'


def upgrade():
    op.create_table('publications', sa.Column('id', sa.String(36), primary_key=True), sa.Column('principal', sa.String(128), nullable=False), sa.Column('idempotency_key', sa.String(128), nullable=False), sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('payload', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.UniqueConstraint('principal', 'idempotency_key'))
    op.create_table('publication_targets', sa.Column('id', sa.String(36), primary_key=True), sa.Column('publication_id', sa.String(36), sa.ForeignKey('publications.id'), nullable=False), sa.Column('account_id', sa.String(64), sa.ForeignKey('accounts.id'), nullable=False), sa.Column('state', sa.String(32), nullable=False), sa.Column('checkpoint', sa.JSON(), nullable=False), sa.Column('result', sa.JSON(), nullable=False), sa.Column('error_code', sa.String(64)), sa.Column('error_message', sa.String(256)), sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('lease_token', sa.String(36)), sa.Column('lease_until', sa.DateTime()), sa.Column('next_attempt_at', sa.DateTime()), sa.UniqueConstraint('publication_id', 'account_id'))
    op.create_table('publication_approvals', sa.Column('id', sa.String(36), primary_key=True), sa.Column('publication_id', sa.String(36), sa.ForeignKey('publications.id'), unique=True, nullable=False), sa.Column('principal', sa.String(128), nullable=False), sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('issuer', sa.String(128), nullable=False), sa.Column('evidence_hash', sa.String(64), nullable=False), sa.Column('issued_at', sa.DateTime(), nullable=False), sa.Column('expires_at', sa.DateTime(), nullable=False), sa.Column('used_at', sa.DateTime()))
    op.create_table('publication_assets', sa.Column('publication_id', sa.String(36), sa.ForeignKey('publications.id'), primary_key=True), sa.Column('media_id', sa.String(36), sa.ForeignKey('media.id'), primary_key=True))


def downgrade():
    for name in ['publication_assets', 'publication_approvals', 'publication_targets', 'publications']:
        op.drop_table(name)
