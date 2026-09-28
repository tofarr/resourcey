# Example 6 — The File Store

A file store built on `resourcey`'s pre-signed-URL API: file **bytes** live in a
pluggable medium, the client transfers them **directly** against a short-lived
capability URL, and the API owns only the **metadata** and the **authorization**.

It is deliberately small — one metadata resource, `files` — because the point is
the handshake, not the business domain. On the happy path the bytes never pass
through a request handler: the API mints a capability, and the client talks to the
medium directly.

## The handshake

```
1. POST /files                    → 201  metadata row (pending), opaque key assigned
2. POST /files/{id}/upload-url    → 200  a short-lived `put` capability URL
3. PUT  <capability url>          → 200  the client transfers the bytes (to the medium)
4. POST /files/{id}/complete      → 200  head + verify + flip the row to `ready`
5. GET  /files/{id}/download      → 200  a short-lived `get` capability URL (ready only)
   GET  <capability url>          → 200  the bytes
```

`POST /files`, `GET /files`, `PATCH`, `DELETE`, `count`, `batch-read` /
`batch-edit` are the ordinary generated actions — metadata is a normal
`SqlResource`.

## What the medium decides

The medium is a `FileStore`, selected from config with **no code change**
(`MEDIUM_CLASS`; unset → `LocalFileStore`). It decides what the capability URL
*is*, and the client handshake is identical across media:

| Medium | Transfer URL | Bytes served by |
| ------ | ------------ | --------------- |
| `S3FileStore` | S3's **native** SigV4 pre-signed URL | S3 directly (no API endpoint) |
| `LocalFileStore` | a **framework-signed** JWE capability | this app's `/_files/{key}` `PUT` / `GET` |
| `SqlFileStore` | a **framework-signed** JWE capability | this app's `/_files/{key}` `PUT` / `GET` |

For S3 the URL points straight at S3, so `register_file_routes` mounts **no**
transfer endpoint at all. For the local / SQL media the framework mints its own
capability (an encrypted token bound to one `(key, operation)` pair) served by the
two `/_files/{key}` routes.

## Layout

```
06_filestore/
├── README.md                 # this file
├── pyproject.toml            # standalone — resourcey from git (parent checkout in-repo)
├── alembic.ini               # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                      # APP_* config (committed); pass with --env-file
├── .gitignore
├── file_store_example/       # the importable app package
│   ├── app.py                # manager + medium + manifest + handshake routes (uvicorn target)
│   ├── models.py             # re-exports the framework's FileMetadata schema (the table)
│   └── files.py              # build_files_resource: the `files` metadata resource
├── migrations/
│   ├── env.py                # Alembic env, diffs against the ORM metadata
│   └── versions/             # generated + reviewed revisions
└── tests/
    ├── test_smoke.py         # handshake over in-memory SQLite (httpx ASGI)
    └── test_e2e.py           # full suite against the committed migration
```

The `files` table is the framework's conventional `FileMetadata` model
(`resourcey.filestore.file_metadata`), re-exported through `models.py` so Alembic
and the tests have a single schema import. `file_resource(store, ...)` serves it
with the server-owned `key` / `status` behaviour, the object cleanup on delete,
the size cap, and a strong-ETag cache policy. An app that needs its own metadata
columns declares its own model and reuses the same handshake helpers.

## Run it

A **standalone** `uv` project. From within `06_filestore`:

```bash
uv sync
uv run --env-file .env alembic upgrade head          # creates file_store_example.db
uv run uvicorn file_store_example.app:app --env-file .env --reload --port 8086
# → Uvicorn running on http://127.0.0.1:8086
```

Interactive docs are at `http://127.0.0.1:8086/docs`.

### Running inside the resourcey repository

`pyproject.toml` depends on `resourcey` from GitHub `main` but adds a
`[tool.uv.sources]` override to the parent checkout (`../..`), so `uv sync` builds
the working tree (your branch or PR). A copy placed outside the repo can no longer
resolve that path — use `uv sync --no-sources` to fall back to the git dependency.

### Testing

```bash
uv sync --extra test
uv run pytest
```

## Example requests

Create the metadata row (declaring the size and MIME type up front):

```bash
curl -s -X POST http://127.0.0.1:8086/files \
  -H "Content-Type: application/json" \
  -d '{"name":"notes.txt","content_type":"text/plain","size":5}'
# → {"id":"…","name":"notes.txt","content_type":"text/plain","size":5,
#    "checksum":null,"etag":null,"status":"pending","created_at":"…","updated_at":"…"}
```

Mint an upload URL, then put the bytes against it:

```bash
FILE_ID=…                       # the id from the create response
UPLOAD=$(curl -s -X POST http://127.0.0.1:8086/files/$FILE_ID/upload-url)
# → {"url":"…/_files/<key>?token=…","method":"PUT","expires_at":"…","headers":{}}

URL=$(echo "$UPLOAD" | python -c 'import sys,json;print(json.load(sys.stdin)["url"])')
curl -s -X PUT "$URL" -H "Content-Type: text/plain" --data-binary "hello"
```

Complete it, then download it:

```bash
curl -s -X POST http://127.0.0.1:8086/files/$FILE_ID/complete
# → {… "status":"ready","etag":"\"…\"" …}   (the em dash is the medium's ETag)

DOWNLOAD=$(curl -s http://127.0.0.1:8086/files/$FILE_ID/download)
URL=$(echo "$DOWNLOAD" | python -c 'import sys,json;print(json.load(sys.stdin)["url"])')
curl -s "$URL"
# → hello
```

With the default local medium the capability URL is relative to the API host. For
a cross-origin client set `MEDIUM_SIGNED_URL_BASE_URL` (e.g.
`http://127.0.0.1:8086`) so the minted URL is absolute.

The metadata surface is the standard one:

```bash
curl -s "http://127.0.0.1:8086/files?content_type__eq=text/plain"   # search
curl -s http://127.0.0.1:8086/files/count                           # count
curl -s -X PATCH http://127.0.0.1:8086/files/$FILE_ID \
  -H "Content-Type: application/json" -d '{"name":"renamed.txt"}'  # update
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8086/files/$FILE_ID
# → 204   (also removes the stored object)
```

## Configuration

The framework reads the process-wide `APP` prefix and does **no** `.env` loading,
so every command passes `--env-file .env`.

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `APP_SQL_CONNECTIONS_0_NAME` | `main` | Connection name (first is the default). |
| `APP_SQL_CONNECTIONS_0_URL` | `sqlite+aiosqlite:///file_store_example.db` | Complete SQLAlchemy URL (driver in scheme). |
| `APP_ENCRYPTION_KEY_ID` | `dev` | Cursor / capability key id. |
| `APP_ENCRYPTION_KEY_VALUE` | *(dev default + warning when unset)* | Cursor / capability key value. |
| `MEDIUM_CLASS` | *(unset → `LocalFileStore`)* | Dotted path of the `FileStore` subclass. |
| `MEDIUM_ROOT` | `./.resourcey_files` | Local medium's byte directory. |
| `MEDIUM_BUCKET` / `MEDIUM_REGION` / `MEDIUM_PREFIX` | — | S3 medium's bucket / region / key prefix. |
| `MEDIUM_CONNECTION_NAME` | `main` | SQL blob medium's connection. |
| `APP_UPLOAD_URL_TTL_SECONDS` | `900` | How long a `put` capability stays valid. |
| `APP_DOWNLOAD_URL_TTL_SECONDS` | `900` | How long a `get` capability stays valid. |
| `APP_MAX_SIZE` | *(unset)* | Cap on a declared upload size, in bytes. |

### Switching media

**S3** (needs the extra — `uv add 'resourcey[s3]'` for a standalone copy):

```bash
MEDIUM_CLASS=resourcey.filestore.s3_file_store.S3FileStore
MEDIUM_BUCKET=my-bucket
MEDIUM_REGION=us-east-1
# optional: MEDIUM_ENDPOINT_URL (MinIO), MEDIUM_PREFIX, MEDIUM_ACCESS_KEY_ID / _SECRET_ACCESS_KEY
```

The handshake is unchanged: step 3's URL is now an S3 pre-signed URL and the
bytes go straight to the bucket. The API mounts no transfer endpoint, and
`complete` heads the object with S3 to fill in size / MIME / ETag.

**SQL blob table** (`file_blobs` on the app's own connection):

```bash
MEDIUM_CLASS=resourcey.filestore.sql_file_store.SqlFileStore
MEDIUM_CONNECTION_NAME=main
```

The blob table is storage, not a resource — it is never registered and never
derived into a read model. Create it with
`create_blob_tables(session_factory)` (or your own Alembic revision against
`resourcey.filestore.sql_file_store.FileBlobBase`); it is intentionally **not**
in this example's committed migration, which covers only the `files` table.

## Security notes

* A capability URL is a **bearer token**: anyone holding it can use it until it
  expires. Keep the TTLs short; treat the URL like a credential.
* It is bound to exactly one `(key, operation)` pair, so a `get` token cannot be
  replayed as a `put`, nor against another object's key.
* The storage `key` is server-assigned and hidden from every response — a client
  addresses a file by `id`, never by its key.
* The local medium rejects any key that is empty, absolute, `~`, or contains
  `..`, and re-checks the resolved path stays under `root`.
* Minting is **authorized**: the handshake handlers resolve the metadata through
  the resource's normal `DependencyBuilder` seam, so to secure the app pass an
  `AuthorizedDependencyBuilder` as `dependency_builder=` (exactly as examples
  03–05 do) and every mint requires permission on the file. This example ships
  the open (`OpenDependencyBuilder`) default for clarity.

## Auto-generated REST surface

| Method | Path | Action |
| ------ | ---- | ------ |
| `POST` | `/files` | create |
| `GET` | `/files/{id}` | read |
| `PATCH` | `/files/{id}` | update |
| `DELETE` | `/files/{id}` | delete |
| `GET` | `/files` | search |
| `GET` | `/files/count` | count |
| `GET` | `/files/batch-read` | batch_read |
| `POST` | `/files/batch-edit` | batch_edit |

Plus the handshake routes `POST /files/{id}/upload-url`,
`POST /files/{id}/complete`, `GET /files/{id}/download`, and — for the local /
SQL media only — the framework-signed transfer routes `PUT` / `GET`
`/_files/{key}` (hidden from the schema).
