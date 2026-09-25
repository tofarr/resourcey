"""``DbConfig`` — one named SQL connection (issue #74).

A plain :class:`~pydantic.BaseModel` (nested inside
:class:`~resourcey.v2.sql.sql_config.SqlConfig`), so it carries no instance
cache and no env prefix of its own: it is parsed as part of its parent's
``APP_SQL_CONNECTIONS_<n>_*`` slice.

The password is a separate field rather than being embedded in the URL so a
secret store can populate it independently (e.g. from
``APP_SQL_CONNECTIONS_0_PASSWORD`` encrypted at rest with SOPS) while the
plaintext URL comes from an ordinary env var.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.engine import make_url


class DbConfig(BaseModel):
    """One named SQL connection: a URL plus an optional separate password.

    The URL scheme selects the driver (``postgresql+asyncpg``,
    ``sqlite+aiosqlite``, ...). :attr:`name` is required so every connection is
    addressable by name; a resource that names none uses the first listed.
    """

    name: str = Field(
        description=(
            "Connection name, used to select this connection from a resource "
            "(``SqlResource(Model, name='reports')``). Required and unique "
            "within a config."
        ),
    )
    url: str = Field(
        default="sqlite+aiosqlite:///app.db",
        description=(
            "SQL connection URL; the scheme selects the driver, e.g. "
            "'postgresql+asyncpg://user@host:port/db' or 'sqlite+aiosqlite:///path.db'."
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
        """The connection URL with :attr:`password` spliced in when set.

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
