"""users and permissions init

Revision ID: 0001_init
Revises:
Create Date: 2026-09-21 16:00:00.000000

"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_init"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the auth + resource tables and seed two users with permissions."""
    # --- auth tables (hand-written ORM models on AuthBase) -------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("password", sa.String(length=2048), nullable=True),
        sa.Column("idp_user_id", sa.String(length=255), nullable=True),
        # The resource-generated ``users`` table adds ``creator_id`` so
        # ``CreatorPermission`` can scope "edit your own record".
        sa.Column("creator_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("username"),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_idp_user_id", "users", ["idp_user_id"], unique=False)
    op.create_index("ix_users_creator_id", "users", ["creator_id"], unique=False)

    op.create_table(
        "user_permissions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("permission", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_permissions_user_id", "user_permissions", ["user_id"], unique=False)
    op.create_index(
        "ix_user_permissions_resource_type", "user_permissions", ["resource_type"], unique=False
    )

    op.create_table(
        "idp_refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_token", sa.String(length=8192), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_idp_refresh_tokens_creator_id", "idp_refresh_tokens", ["creator_id"], unique=False
    )

    op.create_table(
        "idp_access_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("refresh_token_id", sa.Uuid(), nullable=False),
        sa.Column("access_token", sa.String(length=8192), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["refresh_token_id"], ["idp_refresh_tokens.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_idp_access_tokens_refresh_token_id",
        "idp_access_tokens",
        ["refresh_token_id"],
        unique=False,
    )

    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("client_id", sa.String(length=128), nullable=False),
        sa.Column("client_secret", sa.String(length=8192), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index("ix_oauth_clients_client_id", "oauth_clients", ["client_id"], unique=True)

    op.create_table(
        "oauth_client_redirect_uris",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_oauth_client_redirect_uris_client_id",
        "oauth_client_redirect_uris",
        ["client_id"],
        unique=False,
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_creator_id", "api_keys", ["creator_id"], unique=False)

    # --- resource tables (generated on ResourceyBase) -----------------------
    op.create_table(
        "threads",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("creator_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
    )
    op.create_index("ix_threads_created_at", "threads", ["created_at"], unique=False)
    op.create_index("ix_threads_updated_at", "threads", ["updated_at"], unique=False)
    op.create_index("ix_threads_creator_id", "threads", ["creator_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"]),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"]),
    )
    op.create_index("ix_messages_created_at", "messages", ["created_at"], unique=False)
    op.create_index("ix_messages_updated_at", "messages", ["updated_at"], unique=False)
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"], unique=False)
    op.create_index("ix_messages_creator_id", "messages", ["creator_id"], unique=False)

    # --- seed: admin + regular user with permission rows --------------------
    _seed()


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    op.drop_table("messages")
    op.drop_table("threads")
    op.drop_table("api_keys")
    op.drop_table("oauth_client_redirect_uris")
    op.drop_table("oauth_clients")
    op.drop_table("idp_access_tokens")
    op.drop_table("idp_refresh_tokens")
    op.drop_table("user_permissions")
    op.drop_table("users")


# --------------------------------------------------------------------------- #
# Seed data.
# --------------------------------------------------------------------------- #

# Stable UUIDs so the seeded users are addressable from tests / docs. In a real
# app these would be generated at migration time; fixed values keep the example
# reproducible.
ADMIN_ID = "00000000-0000-0000-0000-000000000001"
REGULAR_ID = "00000000-0000-0000-0000-000000000002"


def _seed() -> None:
    """Insert an admin (Permitted on everything) and a regular user.

    Passwords are bcrypt hashes; the dev IdP login (`POST /auth/dev/login`)
    verifies them. ``creator_id`` is set to each user's own id so
    ``CreatorPermission`` (which the regular user holds on ``User``) scopes
    "edit your own record" to the principal.

    Inserts via the resource-generated ORM ``__table__`` objects so SQLAlchemy's
    ``Uuid`` type processors bind the ids correctly across SQLite / Postgres.
    """
    from resourcey.auth.password import hash_password
    from resourcey.auth.permission import CreatorPermission, Permitted

    from users_and_permissions.user import User
    from users_and_permissions.user_permission import UserPermission

    bind = op.get_bind()
    now = datetime.now(UTC)
    admin_hash = hash_password("admin")
    regular_hash = hash_password("regular")
    admin_id = uuid.UUID(ADMIN_ID)
    regular_id = uuid.UUID(REGULAR_ID)

    user_table = User.get_sql_alchemy_model().__table__
    up_table = UserPermission.get_sql_alchemy_model().__table__

    bind.execute(
        user_table.insert().values(
            id=admin_id,
            email="admin@example.com",
            username="admin",
            enabled=True,
            password=admin_hash,
            creator_id=admin_id,
            created_at=now,
            updated_at=now,
        )
    )
    bind.execute(
        user_table.insert().values(
            id=regular_id,
            email="regular@example.com",
            username="regular",
            enabled=True,
            password=regular_hash,
            creator_id=regular_id,
            created_at=now,
            updated_at=now,
        )
    )

    admin_perm = Permitted().model_dump(mode="json")
    own = CreatorPermission(on_match=Permitted(), on_create=Permitted()).model_dump(mode="json")
    rows: list[dict] = []
    for resource_type in ("Thread", "Message", "User", "UserPermission"):
        rows.append(
            {
                "id": _new_id(),
                "user_id": admin_id,
                "resource_type": resource_type,
                "permission": admin_perm,
                "created_at": now,
                "updated_at": now,
            }
        )
    for resource_type in ("Thread", "Message", "User"):
        rows.append(
            {
                "id": _new_id(),
                "user_id": regular_id,
                "resource_type": resource_type,
                "permission": own,
                "created_at": now,
                "updated_at": now,
            }
        )
    bind.execute(up_table.insert(), rows)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()
