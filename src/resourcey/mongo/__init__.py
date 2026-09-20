"""MongoDB resource backend (optional extra, issue #47).

Provides ``MongoResource`` and ``MongoService`` — a non-SQL implementation of
the resourcey resource/service contract backed by a MongoDB collection via the
async ``motor`` driver. Subclass ``MongoResource`` directly (no SQLAlchemy).

This package is an **optional extra** (``pip install resourcey[mongodb]``).
The core package never imports ``motor``; importing this package without the
extra installed raises a clear ``ImportError`` pointing to the extra.

A non-SQL resource does **not** participate in Alembic migrations. Instead,
:meth:`~resourcey.mongo.mongo_resource.MongoResource.migrate_document` is an
opt-in hook (default no-op) invoked on read so an application can lazily
upgrade a document to the current shape. The versioning scheme is
application-defined — most implementations carry a schema-version field, but
the framework does not prescribe it.
"""

from __future__ import annotations


def _require_motor() -> None:
    """Eagerly fail with an actionable error when the ``mongodb`` extra is missing.

    Importing ``resourcey.mongo`` without ``motor`` installed is a packaging
    error, not a runtime bug: surface a message naming the extra rather than
    an opaque ``ModuleNotFoundError`` deep in a request handler.
    """
    try:
        import motor  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "resourcey.mongo requires the 'mongodb' extra. Install it with "
            "`pip install resourcey[mongodb]` or `uv sync --extra mongodb`."
        ) from exc


_require_motor()
