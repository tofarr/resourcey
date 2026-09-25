"""``SqlConfig`` — the app's named SQL connections (issue #74).

Parsed from the process-wide prefix (``APP`` by default): connection ``n``
reads ``APP_SQL_CONNECTIONS_<n>_NAME`` / ``_URL`` / ``_PASSWORD``. The field is
named ``sql_connections`` rather than ``connections`` so a future non-SQL
config (e.g. Mongo) can use ``connections`` without a name collision.

Names are required and must be unique (and non-empty): a connection lookup is
an exact, case-sensitive match, so a duplicate or blank name has no defined
target. Both are rejected here — at config build — rather than at lookup time.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from resourcey.v2.config.config_base import BaseConfig
from resourcey.v2.sql.db_config import DbConfig


class SqlConfig(BaseConfig):
    """The app's SQL connection list, parsed under the process-wide prefix."""

    sql_connections: list[DbConfig] = Field(
        default_factory=list,
        description=(
            "Named SQL connections. A resource with no explicit name uses the "
            "first; an unknown name raises ResourceyConfigError at first use. "
            "An empty list has no default connection."
        ),
    )

    @model_validator(mode="after")
    def _validate_connection_names(self) -> SqlConfig:
        """Reject blank or duplicate connection names.

        A blank name is a misconfiguration, not "unnamed" (consistent with
        ``LazyField``'s set-but-empty rule); duplicates make the lookup
        ambiguous. Both raise ``ValueError``, which :meth:`get_instance` maps
        to :class:`~resourcey.v2.core.errors.ResourceyConfigError`.
        """
        seen: set[str] = set()
        for connection in self.sql_connections:
            name = connection.name.strip()
            if not name:
                raise ValueError("A SQL connection name must not be empty")
            if name in seen:
                raise ValueError(f"Duplicate SQL connection name {connection.name!r}")
            seen.add(name)
        return self
