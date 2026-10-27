"""Distinguish policy reliance from independent owner grants; optional portal sessions."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'


def upgrade():
    op.add_column('publication_approvals', sa.Column('channel', sa.String(32), nullable=False, server_default='owner_cli'))
    op.add_column('publication_approvals', sa.Column('oauth_client_id', sa.String(128)))
    op.create_table('approval_portal_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('ticket_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('browser_hash', sa.String(64), unique=True),
        sa.Column('owner', sa.String(128), nullable=False),
        sa.Column('consumed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False))
    op.create_index('ix_approval_portal_sessions_expires_at', 'approval_portal_sessions', ['expires_at'])


def downgrade():
    op.drop_table('approval_portal_sessions')
    op.drop_column('publication_approvals', 'oauth_client_id')
    op.drop_column('publication_approvals', 'channel')
