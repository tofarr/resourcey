"""The framework's own configuration.

:class:`FrameworkConfig` is a :class:`~resourcey.config.config_base.BaseConfig`
parsed with the ``RESOURCEY`` prefix. The database connection is a structured
:class:`DbConfig` nested model (rather than a single connection string) so
each component can be injected independently — e.g. a secret store populates
``password`` while the rest comes from plaintext env vars.

The :attr:`resources` field carries the registered ``BaseResource`` subclasses
this app serves. Its env representation is a list of dotted import paths — a
JSON array (``RESOURCEY_RESOURCES=["myapp.user.User","myapp.rbac.Role"]``) or
the sequential form (``RESOURCEY_RESOURCES_0`` / ``RESOURCEY_RESOURCES_1`` …)
— resolved to the classes lazily on first access via :class:`LazyField`, never
at import time. This makes the config the single source of truth for "what
does this app serve", and doubles as the mechanism the migrations CLI (#3)
needs to ensure all resource modules are imported before ``env.py`` runs.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from resourcey.config.config_base import BaseConfig
from resourcey.config.lazy_field import LazyField
from resourcey.resource.base import BaseResource

MIGRATIONS_DIR_DEFAULT = "migrations"


class DbConfig(BaseModel):
    """Structured database connection configuration.

    The SQLAlchemy async URL is assembled from these fields rather than read
    as a single connection string, so each component can be injected
    independently. Use the :attr:`database_url` property to get the assembled
    ``postgresql+asyncpg`` URL.
    """

    protocol: str = Field(default="postgresql+asyncpg", description="Database driver protocol.")
    host: str = Field(default="localhost", description="Database host.")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port.")
    db_name: str = Field(default="resourcey", description="Database name.")
    username: str = Field(default="resourcey", description="Database username.")
    password: str = Field(default="resourcey", description="Database password.")

    @property
    def database_url(self) -> str:
        """Assemble the async SQLAlchemy URL from the structured fields."""
        return (
            f"{self.protocol}://{self.username}:"
            f"{self.password}@{self.host}:{self.port}/{self.db_name}"
        )


class MigrationConfig(BaseModel):
    """Alembic migration configuration.

    ``migrations_dir`` is the directory (relative to the working directory
    unless absolute) that holds ``env.py`` and the ``versions/`` revisions.
    The resource set is read from :attr:`FrameworkConfig.resources` (the
    app-level source of truth) — migrations register those classes via
    :func:`resourcey.resource.registry.register_resource` so Alembic's
    autogeneration sees every table before diffing.
    """

    migrations_dir: str = Field(
        default=MIGRATIONS_DIR_DEFAULT,
        description="Directory holding alembic env.py and versions/ (relative or absolute).",
    )


class FrameworkConfig(BaseConfig):
    """Top-level framework configuration (prefix ``RESOURCEY``)."""

    @classmethod
    def get_prefix(cls) -> str:
        return "RESOURCEY"

    database: DbConfig = Field(
        default_factory=DbConfig, description="Database connection configuration."
    )
    migrations: MigrationConfig = Field(
        default_factory=MigrationConfig, description="Alembic migration configuration."
    )
    debug: bool = Field(default=False, description="Enable debug mode.")
    host: str = Field(default="127.0.0.1", description="App server (uvicorn) host.")
    port: int = Field(default=8000, ge=1, le=65535, description="App server (uvicorn) port.")
    cors_origins: list[str] = Field(
        default_factory=list, description="Allowed CORS origins (JSON array or sequential indices)."
    )
    # LazyField is resolved on first access (get_type_hints + env read), so
    # importing this module never imports the resource modules. The env
    # representation is a JSON array (``RESOURCEY_RESOURCES``) or the
    # sequential form (``RESOURCEY_RESOURCES_0`` / ``_1`` …) of dotted
    # import paths.
    resources: ClassVar[list[type[BaseResource]]] = LazyField()  # type: ignore[assignment]
