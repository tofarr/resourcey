"""The framework's own configuration.

:class:`FrameworkConfig` is a :class:`~resourcey.config.config_base.BaseConfig`
parsed with the ``RESOURCEY`` prefix. The database connection is a single
:class:`DbConfig` nested model holding the connection :attr:`DbConfig.url` plus
an optional :attr:`DbConfig.password`. The password is a separate field rather
than being embedded in the URL so a secret store can populate it
independently — e.g. an ``RESOURCEY_DATABASE_PASSWORD`` env var encrypted at
rest with a tool such as SOPS, while the plaintext URL comes from ordinary env
vars.

There is one connection, not one per backend: an app talks to a SQL database or
MongoDB, never both in the same app. The URL scheme selects the driver
(``postgresql+asyncpg``, ``sqlite+aiosqlite``, ``mongodb``, ...); the
``embedded`` scheme selects the in-process mongomock client used for local
development and tests.

The :attr:`manifest` field is a dotted/colon import path
(``module:attr``) to the app's :class:`~resourcey.manifest.ResourceManifest`
instance. The framework resolves it lazily (e.g. in the Alembic ``env.py``)
to materialise the resource tables before migrations diff. This is the single
source of truth for "what does this app serve".
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.engine import make_url

from resourcey.config.config_base import BaseConfig
from resourcey.config.config_dependency import DefaultDependencyBuilder, DependencyBuilder
from resourcey.config.lazy_field import LazyField

MIGRATIONS_DIR_DEFAULT = "migrations"


class DbConfig(BaseModel):
    """Database connection configuration.

    Holds the connection :attr:`url` and an optional :attr:`password`, kept
    separate so the secret can be injected from its own env var (and encrypted
    independently). The URL scheme selects the backend and driver:

    * ``postgresql+asyncpg://user@host:port/db`` — SQL via SQLAlchemy.
    * ``sqlite+aiosqlite:///path/to/file.db`` — SQLite (URL is used verbatim).
    * ``mongodb://user@host:port/db`` — MongoDB via ``motor``, the database name
      read from the URL path (like any Mongo connection string).
    * ``embedded://<db>`` — the in-process mongomock client (no external
      server), for local development and tests. The host component names the
      database (``embedded://message_board``); a bare ``embedded`` uses the
      default database name.

    Use :attr:`database_url` for the SQL (SQLAlchemy) view of the connection,
    and :attr:`mongo_password` / :meth:`mongo_database_name` for the Mongo view.
    """

    url: str = Field(
        default="postgresql+asyncpg://resourcey@localhost:5432/resourcey",
        description=(
            "Database connection URL. The scheme selects the backend/driver: "
            "'postgresql+asyncpg://user@host:port/db' or 'sqlite+aiosqlite:///path.db' "
            "for SQL, 'mongodb://user@host:port/db' for MongoDB, or "
            "'embedded://<db>' for the in-process mongomock client."
        ),
    )
    password: SecretStr | None = Field(
        default=None,
        description=(
            "Database password, injected separately from the URL so a secret "
            "store can supply (and encrypt) it on its own. When None the "
            "password embedded in the URL — if any — is used as-is."
        ),
    )

    @property
    def database_url(self) -> str:
        """The SQL connection URL with :attr:`password` spliced in when set.

        Returns a plain ``str`` (SQLAlchemy accepts a string URL). The password
        is spliced with SQLAlchemy's URL parser so reserved characters are
        percent-encoded rather than corrupting the URL. When :attr:`password`
        is ``None`` the URL is returned exactly as configured (so a password
        embedded in the URL still works).
        """
        if self.password is None:
            return self.url
        spliced = make_url(self.url).set(password=self.password.get_secret_value())
        return spliced.render_as_string(hide_password=False)

    @property
    def mongo_password(self) -> str | None:
        """The plaintext password for the Mongo driver, or ``None`` when unset.

        Mongo receives the password as a client kwarg rather than spliced into
        the URL: ``motor`` (via ``pymongo``) treats it as a separate connection
        option, and passing it explicitly *overrides* any password in the URL.
        The caller must therefore pass it only when not ``None`` — passing
        ``password=None`` is not neutral, it clears a URL-embedded password.
        """
        return self.password.get_secret_value() if self.password is not None else None

    @property
    def is_embedded(self) -> bool:
        """Whether the URL selects the in-process mongomock client.

        True for the bare ``embedded`` marker and any ``embedded://`` URL (whose
        host component names the database).
        """
        return self.url == "embedded" or self.url.startswith("embedded://")

    def mongo_database_name(self, default: str) -> str:
        """The Mongo database name: the URL's database component, else ``default``.

        For ``embedded://<name>`` the name is the host component (a bare
        ``embedded`` has none). For a real ``mongodb://`` URL it is the first
        path segment — the standard Mongo convention — parsed from the string
        rather than via a driver so a multi-host URL
        (``mongodb://h1,h2/db``) needs no connection or DNS.
        """
        if self.url == "embedded":
            return default
        if self.url.startswith("embedded://"):
            return self.url[len("embedded://") :] or default
        path = urlsplit(self.url).path.lstrip("/")
        return path.split("/", 1)[0] or default


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

    # The per-request service dependency seam (issue #62). A LazyField so the
    # concrete builder (which may live in an app / auth package the config must
    # not import) is loaded only on first access. Defaults to
    # DefaultDependencyBuilder, preserving pre-#62 behaviour. Follows the
    # single-class LazyField convention and reads ``DEPENDENCY_BUILDER_CLASS``
    # (the field name only, not the RESOURCEY_ prefix — see the list variant's
    # get_prefix() convention).
    dependency_builder: ClassVar[DependencyBuilder] = LazyField(  # type: ignore[assignment]
        default=DefaultDependencyBuilder
    )

    database: DbConfig = Field(
        default_factory=DbConfig, description="Database connection configuration."
    )
    migrations: MigrationConfig = Field(
        default_factory=MigrationConfig, description="Alembic migration configuration."
    )
    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description="Authentication / session configuration (issue #4).",
    )
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
