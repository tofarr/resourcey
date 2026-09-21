"""The framework's own configuration.

:class:`FrameworkConfig` is a :class:`~resourcey.config.config_base.BaseConfig`
parsed with the ``RESOURCEY`` prefix. The database connection is a structured
:class:`DbConfig` nested model (rather than a single connection string) so
each component can be injected independently — e.g. a secret store populates
``password`` while the rest comes from plaintext env vars.

The :attr:`manifest` field is a dotted/colon import path
(``module:attr``) to the app's :class:`~resourcey.manifest.ResourceManifest`
instance. The framework resolves it lazily (e.g. in the Alembic ``env.py``)
to materialise the resource tables before migrations diff. This is the single
source of truth for "what does this app serve".
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from resourcey.config.config_base import BaseConfig

MIGRATIONS_DIR_DEFAULT = "migrations"


class DbConfig(BaseModel):
    """Structured database connection configuration.

    The SQLAlchemy async URL is assembled from these fields rather than read
    as a single connection string, so each component can be injected
    independently. Use the :attr:`database_url` property to get the assembled
    ``postgresql+asyncpg`` URL.

    For databases whose URL shape doesn't fit the ``protocol://user:pass@
    host:port/db`` pattern (notably SQLite, which is
    ``sqlite+aiosqlite:///path/to/file.db``), set :attr:`full_db_url` to the
    complete URL. When set, it takes precedence over the structured fields.
    """

    protocol: str = Field(default="postgresql+asyncpg", description="Database driver protocol.")
    host: str = Field(default="localhost", description="Database host.")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port.")
    db_name: str = Field(default="resourcey", description="Database name.")
    username: str = Field(default="resourcey", description="Database username.")
    password: str = Field(default="resourcey", description="Database password.")
    full_db_url: str | None = Field(
        default=None,
        description=(
            "Complete SQLAlchemy URL override (takes precedence over the "
            "structured fields). Use for SQLite or any URL that doesn't fit "
            "the protocol://user:pass@host:port/db pattern."
        ),
    )

    @property
    def database_url(self) -> str:
        """Assemble the async SQLAlchemy URL from the structured fields.

        Returns :attr:`full_db_url` verbatim when set (the escape hatch for
        SQLite etc.); otherwise assembles ``protocol://user:pass@host:port/db``.
        """
        if self.full_db_url is not None:
            return self.full_db_url
        return (
            f"{self.protocol}://{self.username}:"
            f"{self.password}@{self.host}:{self.port}/{self.db_name}"
        )


class MongoConfig(BaseModel):
    """MongoDB connection configuration.

    ``url`` is the ``mongodb://`` connection string. When empty or
    ``"embedded"``, :meth:`MongoResource.build_client` uses the in-process
    :class:`~resourcey.mongo.embedded.AsyncEmbeddedClient` (no external
    server) — the default for local development and tests.
    """

    url: str = Field(
        default="embedded",
        description=(
            "MongoDB connection URL, or 'embedded' (the default) for the "
            "in-process mongomock-backed client."
        ),
    )
    database: str = Field(
        default="resourcey",
        description="MongoDB database name.",
    )


class MigrationConfig(BaseModel):
    """Alembic migration configuration.

    ``migrations_dir`` is the directory (relative to the working directory
    unless absolute) that holds ``env.py`` and the ``versions/`` revisions.
    The resource set is read from :attr:`FrameworkConfig.manifest` (the
    app-level source of truth) — ``env.py`` resolves and materialises the
    manifest so Alembic's autogeneration sees every table before diffing.
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
    mongo: MongoConfig = Field(
        default_factory=MongoConfig, description="MongoDB connection configuration."
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
    # The manifest is a dotted/colon import path (``module:attr``) pointing at
    # the app's :class:`~resourcey.manifest.ResourceManifest` instance. The
    # migrations CLI uses it to import the manifest (and materialise its tables)
    # before Alembic diffs. This is config-as-discovery, not config-as-definition:
    # the manifest instance owns the resource set, not config.
    manifest: str = Field(default="", description="Dotted/colon path to the app's ResourceManifest.")
