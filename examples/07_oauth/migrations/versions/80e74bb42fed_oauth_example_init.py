"""oauth example init

Revision ID: 80e74bb42fed
Revises:
Create Date: 2026-09-30 11:58:06.702109

Creates the whole example schema in one revision: the board (``threads`` /
``messages``), the local principal store (``users``), and the framework's OAuth
tables (``oauth_clients`` / ``external_identities`` / ``oauth_tokens``). It then
seeds the two local users and the ``(issuer, subject) -> user_id`` links the dev
tokens resolve to, so ``alembic upgrade head`` is sufficient to run the example.
"""
from typing import Sequence, Union

from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Uuid
from sqlalchemy.dialects import postgresql

from oauth_example.seed import SEED_IDENTITIES, SEED_USERS, _identity_id

# revision identifiers, used by Alembic.
revision: str = '80e74bb42fed'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the schema and seed the local users + identity links."""
    op.create_table('external_identities',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('issuer', sa.String(length=512), nullable=False),
    sa.Column('subject', sa.String(length=512), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('issuer', 'subject', name='uq_external_identity')
    )
    op.create_index(op.f('ix_external_identities_issuer'), 'external_identities', ['issuer'], unique=False)
    op.create_index(op.f('ix_external_identities_user_id'), 'external_identities', ['user_id'], unique=False)
    op.create_table('oauth_clients',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('provider', sa.String(length=128), nullable=True),
    sa.Column('issuer', sa.String(length=512), nullable=False),
    sa.Column('jwks_uri', sa.String(length=1024), nullable=True),
    sa.Column('audience', sa.String(length=512), nullable=True),
    sa.Column('algorithms', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('client_id', sa.String(length=512), nullable=True),
    sa.Column('client_secret', sa.String(length=1024), nullable=True),
    sa.Column('auth_url', sa.String(length=1024), nullable=True),
    sa.Column('token_url', sa.String(length=1024), nullable=True),
    sa.Column('refresh_url', sa.String(length=1024), nullable=True),
    sa.Column('redirect_uri', sa.String(length=1024), nullable=True),
    sa.Column('scopes', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('roles', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('refresh_rotates_token', sa.Boolean(), nullable=False),
    sa.Column('refresh_is_single_use', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('issuer', name='uq_oauth_client_issuer')
    )
    op.create_index(op.f('ix_oauth_clients_issuer'), 'oauth_clients', ['issuer'], unique=False)
    op.create_table('oauth_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('principal_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.String(length=128), nullable=False),
    sa.Column('access_token', sa.String(length=8192), nullable=False),
    sa.Column('refresh_token', sa.String(length=8192), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scope', sa.String(length=2048), nullable=True),
    sa.Column('lease_owner', sa.String(length=128), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('principal_id', 'client_id', name='uq_oauth_token')
    )
    op.create_index(op.f('ix_oauth_tokens_client_id'), 'oauth_tokens', ['client_id'], unique=False)
    op.create_index(op.f('ix_oauth_tokens_principal_id'), 'oauth_tokens', ['principal_id'], unique=False)
    op.create_table('threads',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    op.create_table('messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('thread_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Uuid(), nullable=True),
    sa.Column('text', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['thread_id'], ['threads.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messages_author_id'), 'messages', ['author_id'], unique=False)
    op.create_index(op.f('ix_messages_thread_id'), 'messages', ['thread_id'], unique=False)

    _seed()


def _seed() -> None:
    """Insert the fixed local users and the identity links the dev tokens resolve to."""
    now = datetime.now(UTC)
    users = sa.table(
        'users',
        sa.column('id', Uuid()),
        sa.column('email', sa.String()),
        sa.column('username', sa.String()),
        sa.column('enabled', sa.Boolean()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )
    op.bulk_insert(
        users,
        [{**spec, 'created_at': now, 'updated_at': now} for spec in SEED_USERS],
    )
    identities = sa.table(
        'external_identities',
        sa.column('id', Uuid()),
        sa.column('issuer', sa.String()),
        sa.column('subject', sa.String()),
        sa.column('user_id', Uuid()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )
    op.bulk_insert(
        identities,
        [
            {
                'id': _identity_id(spec['issuer'], spec['subject']),
                'issuer': spec['issuer'],
                'subject': spec['subject'],
                'user_id': spec['user_id'],
                'created_at': now,
                'updated_at': now,
            }
            for spec in SEED_IDENTITIES
        ],
    )


def downgrade() -> None:
    """Drop the example schema."""
    op.drop_index(op.f('ix_messages_thread_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_author_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_table('threads')
    op.drop_index(op.f('ix_oauth_tokens_principal_id'), table_name='oauth_tokens')
    op.drop_index(op.f('ix_oauth_tokens_client_id'), table_name='oauth_tokens')
    op.drop_table('oauth_tokens')
    op.drop_index(op.f('ix_oauth_clients_issuer'), table_name='oauth_clients')
    op.drop_table('oauth_clients')
    op.drop_index(op.f('ix_external_identities_user_id'), table_name='external_identities')
    op.drop_index(op.f('ix_external_identities_issuer'), table_name='external_identities')
    op.drop_table('external_identities')
