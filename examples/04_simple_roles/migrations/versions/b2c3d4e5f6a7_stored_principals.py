"""stored principals

Revision ID: b2c3d4e5f6a7
Revises: 9633ab111df3
Create Date: 2026-09-28 00:00:00.000000

Adds the ``users`` table — the stored principals the accepted API keys act as —
and seeds the two fixed rows the committed ``.env`` names. Seeding in the
migration keeps ``alembic upgrade head`` sufficient to run the example: the
``APP_API_KEYS_<n>_PRINCIPAL_ID`` values then resolve to real, enabled rows.
"""
from typing import Sequence, Union

from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Uuid

from simple_roles.seed import ADMIN_ID, USER_ID


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = '9633ab111df3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``users`` table and seed the fixed principals."""
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)

    users = sa.table(
        'users',
        sa.column('id', Uuid()),
        sa.column('email', sa.String()),
        sa.column('username', sa.String()),
        sa.column('enabled', sa.Boolean()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        users,
        [
            {
                'id': ADMIN_ID,
                'email': 'admin@example.com',
                'username': 'admin',
                'enabled': True,
                'created_at': now,
                'updated_at': now,
            },
            {
                'id': USER_ID,
                'email': 'user@example.com',
                'username': 'user',
                'enabled': True,
                'created_at': now,
                'updated_at': now,
            },
        ],
    )


def downgrade() -> None:
    """Drop the ``users`` table."""
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
