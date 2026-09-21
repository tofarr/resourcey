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

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

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


class IdpConfig(BaseModel):
    """Federated OAuth (auth) — identity provider configuration.

    The project delegates authentication to an external identity provider
    (OIDC/OAuth2). It acts as an OAuth provider to first-party clients and an
    OAuth client to the IdP. These fields wire the federated flow.

    Ported from ohev2's ``IdpConfig``; adapted to resourcey's config framework.
    """

    url: str = Field(
        default="/auth/dev",
        description=(
            "Base URL of the identity provider. Defaults to the built-in dev "
            "identity provider mounted at '/auth/dev'; set to a real IdP URL "
            "for production."
        ),
    )
    client_id: str = Field(
        default="resourcey",
        description="Client id registered at the identity provider.",
    )
    client_secret: SecretStr = Field(
        default=SecretStr("changeme"),
        description="Client secret registered at the identity provider.",
    )
    expire_drift_tolerance: int = Field(
        default=60,
        ge=0,
        description=(
            "Seconds subtracted from IdP-advertised expiries to avoid treating "
            "a token as valid past its real expiry due to clock drift."
        ),
    )
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "email", "profile"],
        description="OAuth scopes requested from the identity provider.",
    )
    authorize_path: str = Field(
        default="/authorize",
        description="Path appended to idp.url for the authorization endpoint.",
    )
    token_path: str = Field(
        default="/token",
        description="Path appended to idp.url for the token exchange endpoint.",
    )
    refresh_path: str = Field(
        default="/token",
        description="Path appended to idp.url for the refresh-token exchange endpoint.",
    )
    revocation_path: str | None = Field(
        default=None,
        description="Path appended to idp.url for RFC 7009 token revocation. None = local-only.",
    )
    access_token_expires_in: int = Field(
        default=900,
        ge=1,
        description="Fallback access-token lifetime (seconds) when the IdP does not advertise one.",
    )
    refresh_token_expires_in: int = Field(
        default=2_592_000,
        ge=1,
        description="Fallback refresh-token lifetime (seconds) when the IdP does not advertise one.",
    )
    refresh_lock_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Max seconds to wait for the refresh-row lock during a concurrent IdP token refresh.",
    )
    sync_api_keys: bool = Field(
        default=False,
        description="When true, an API key is only accepted if the user has a live IdP session.",
    )


class AuthConfig(BaseModel):
    """Authentication / session configuration (issue #4).

    Controls the session cookie attributes and the IdP integration. Ported
    from ohev2's auth-related ``AppConfig`` fields.
    """

    cookie_name: str = Field(
        default="resourcey_session",
        description="Name of the HTTP-only session cookie set by the auth callback.",
    )
    cookie_secure: bool = Field(
        default=True,
        description="Whether the session cookie requires HTTPS (Secure flag).",
    )
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        default="strict",
        description="SameSite attribute for the session cookie.",
    )
    idp: IdpConfig = Field(
        default_factory=IdpConfig,
        description="Identity provider (OAuth/OIDC) configuration.",
    )
    default_permissions_json: str = Field(
        default="",
        description=(
            "App-level default permission policies as a JSON string: "
            '{"resource_type": [{"kind": "permitted"}, ...]}. '
            "Applied to every principal (including anonymous). "
            "Empty string means no defaults."
        ),
    )

    @property
    def default_permissions(self) -> dict[str, list[dict[str, Any]]]:
        """Parse ``default_permissions_json`` into a dict (empty on error)."""
        import json

        if not self.default_permissions_json:
            return {}
        try:
            parsed = json.loads(self.default_permissions_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed


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
    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description="Authentication / session configuration (issue #4).",
    )
    debug: bool = Field(default=False, description="Enable debug mode.")
    host: str = Field(default="127.0.0.1", description="App server (uvicorn) host.")
    port: int = Field(default=8000, ge=1, le=65535, description="App server (uvicorn) port.")
    base_url: str = Field(
        default="http://localhost:8000",
        description=(
            "Public base URL of the application. Used to build absolute URLs "
            "(OAuth callback, OIDC discovery issuer) behind proxies/ingresses."
        ),
    )
    cors_origins: list[str] = Field(
        default_factory=list, description="Allowed CORS origins (JSON array or sequential indices)."
    )
    # The manifest is a dotted/colon import path (``module:attr``) pointing at
    # the app's :class:`~resourcey.manifest.ResourceManifest` instance. The
    # migrations CLI uses it to import the manifest (and materialise its tables)
    # before Alembic diffs. This is config-as-discovery, not config-as-definition:
    # the manifest instance owns the resource set, not config.
    manifest: str = Field(
        default="", description="Dotted/colon path to the app's ResourceManifest."
    )
