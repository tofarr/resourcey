"""Schema of record for the file-store example.

File **metadata** is a normal, model-first SQL resource, so its table is the
schema of record and the framework infers the DTO and the six REST models from
it. The framework ships the conventional metadata model
(:class:`~resourcey.filestore.file_metadata.FileMetadata`) together with
:func:`~resourcey.filestore.file_resource`, which serves it with the
server-owned ``key`` / ``status`` behaviour, the object cleanup on delete, and an
ETag cache policy — so this example re-exports that model and its declarative
base rather than re-declaring the table.

An app that needs different metadata columns declares its own model and reuses
the handshake helpers; the conventional model is what this example uses.

``Base`` is the metadata table's own declarative base, exactly as an app's own
base would be: it is what Alembic diffs against and what ``create_all`` creates in
the tests. The file **bytes** never appear here — they live in the medium
selected by :class:`~resourcey.filestore.file_config.FileStoreConfig` (a local
directory by default).
"""

from __future__ import annotations

# ``Base`` and ``FileMetadata`` are re-exported so the tests and migrations can
# import the whole schema (base + model) from one module; the metadata base
# already carries the table.
from resourcey.filestore.file_metadata import FileMetadata  # noqa: F401
from resourcey.filestore.file_metadata import FileMetadataBase as Base  # noqa: F401
