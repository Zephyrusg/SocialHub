"""Independent administration sessions and encrypted Meta setup preferences."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'


def upgrade():
    op.create_table('admin_sessions',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('subject', sa.String(128), nullable=False),
        sa.Column('policy_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False))
    op.create_index('ix_admin_sessions_expires_at', 'admin_sessions', ['expires_at'])
    table = op.create_table('site_configuration',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=True))
    op.bulk_insert(table, [{'id': 'meta', 'revision': 0, 'ciphertext': None}])
    op.add_column('oauth_flows', sa.Column('configuration_revision', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('oauth_flows', 'configuration_revision')
    op.drop_table('site_configuration')
    op.drop_table('admin_sessions')
