"""Schema of record for the file-store example -- only when the medium is SQL.

There is **no metadata table** in this design (issue #158): "does the medium
have the bytes" is the only source of truth for a file's existence, so the
default :class:`~resourcey.filestore.local_file_store.LocalFileStore` medium
needs no database at all. The one table this example *can* have is the
optional :class:`~resourcey.filestore.sql_file_store.FileBlob` table, used only
when ``.env`` sets ``MEDIUM_CLASS`` to
:class:`~resourcey.filestore.sql_file_store.SqlFileStore` (bytes in SQL rather
than on a local disk). This module re-exports that model and its declarative
base so the tests and Alembic migration can import the whole schema from one
module, exactly as an app's own base would be.
"""

from __future__ import annotations

# ``Base`` and ``FileBlob`` are re-exported so the tests and migrations can
# import the whole (optional) SQL-medium schema from one module.
from resourcey.filestore.sql_file_store import FileBlob  # noqa: F401
from resourcey.filestore.sql_file_store import FileBlobBase as Base  # noqa: F401
