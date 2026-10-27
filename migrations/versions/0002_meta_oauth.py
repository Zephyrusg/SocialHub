"""One-use, browser-bound Meta OAuth flows; only hashes are persisted."""
from alembic import op
import sqlalchemy as sa
revision = '0002'
down_revision = '0001'


def upgrade():
    op.create_table('oauth_flows',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('ticket_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('state_hash', sa.String(64), unique=True),
        sa.Column('browser_hash', sa.String(64)),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('started', sa.Boolean(), nullable=False),
        sa.Column('consumed', sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table('oauth_flows')
