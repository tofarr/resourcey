"""``MongoConfig`` — the app's named Mongo connections (issue #80).

Mirrors :class:`~resourcey.sql.sql_config.SqlConfig`: connection ``n`` reads
``APP_MONGO_CONNECTIONS_<n>_NAME`` / ``_URL`` / ``_PASSWORD``. The field is named
``mongo_connections`` because the env parser derives each variable from
``{prefix}_{FIELD_NAME}``; ``connections`` is a read-only alias. ``SqlConfig``
uses ``sql_connections``, so the two compose without a collision.

Names are required and must be unique (and non-empty), rejected at config build.

This module imports no code outside the framework.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, model_validator

from resourcey.config.config_base import BaseConfig


class MongoConnectionConfig(BaseModel):
    """One named Mongo connection: a URL plus an optional separate password.

    The URL scheme selects the backend: ``embedded://<db>`` (or the bare
    ``embedded`` marker) builds the in-process ``mongomock`` client, while
    ``mongodb://`` builds a real ``motor`` client.
    """

    name: str = Field(
        description=(
            "Connection name, used to select this connection from a resource "
            "(``MongoResource(ThreadDTO, name='reports')``). Required and unique "
            "within a config."
        ),
    )
    url: str = Field(
        default="embedded://resourcey",
        description=(
            "Mongo connection URL. 'embedded://<db>' (or 'embedded') selects the "
            "in-process mongomock client (no external server); "
            "'mongodb://user@host:port/db' selects a real motor client, with the "
            "database name read from the URL path."
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
    def mongo_password(self) -> str | None:
        """The plaintext password for the Mongo driver, or ``None`` when unset.

        Mongo receives the password as a client kwarg rather than spliced into
        the URL, and passing ``password=None`` is not neutral — it clears a
        URL-embedded password — so a caller must pass it only when not ``None``.
        """
        return self.password.get_secret_value() if self.password is not None else None

    @property
    def is_embedded(self) -> bool:
        """Whether the URL selects the in-process mongomock client."""
        return self.url == "embedded" or self.url.startswith("embedded://")

    def mongo_database_name(self, default: str) -> str:
        """The Mongo database name: the URL's database component, else ``default``.

        Parsed from the string rather than via a driver, so a multi-host URL
        (``mongodb://h1,h2/db``) needs no connection or DNS.
        """
        if self.url == "embedded":
            return default
        if self.url.startswith("embedded://"):
            return self.url[len("embedded://") :] or default
        path = urlsplit(self.url).path.lstrip("/")
        return path.split("/", 1)[0] or default


class MongoConfig(BaseConfig):
    """The app's Mongo connection list, parsed under the process-wide prefix."""

    mongo_connections: list[MongoConnectionConfig] = Field(
        default_factory=list,
        description=(
            "Named Mongo connections (APP_MONGO_CONNECTIONS_<n>_*). A resource "
            "with no explicit name uses the first; an unknown name raises "
            "ResourceyConfigError at first use. An empty list has no default "
            "connection."
        ),
    )

    @property
    def connections(self) -> list[MongoConnectionConfig]:
        """The configured connections (read-only alias for :attr:`mongo_connections`)."""
        return self.mongo_connections

    @model_validator(mode="after")
    def _validate_connection_names(self) -> MongoConfig:
        """Reject blank or duplicate connection names.

        A blank name is a misconfiguration, not "unnamed"; duplicates make the
        lookup ambiguous.
        """
        seen: set[str] = set()
        for connection in self.mongo_connections:
            name = connection.name.strip()
            if not name:
                raise ValueError("A Mongo connection name must not be empty")
            if name in seen:
                raise ValueError(f"Duplicate Mongo connection name {connection.name!r}")
            seen.add(name)
        return self
