"""Browser-bound, encrypted, one-use owner OIDC exchanges."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'


def upgrade():
    op.create_table('owner_oidc_flows',
        sa.Column('state_hash', sa.String(64), primary_key=True),
        sa.Column('browser_hash', sa.String(64), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed', sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table('owner_oidc_flows')
