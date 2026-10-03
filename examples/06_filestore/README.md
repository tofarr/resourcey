# Example 6 — The File Store

A file store built on `resourcey`'s pre-signed-URL API: file **bytes** live in a
pluggable medium, the client transfers them **directly** against a short-lived
capability URL, and the API owns only the **authorization**. There is **no
metadata table** (issue #158): "does the medium have the bytes" is the only
source of truth for a file's existence, so a file simply does not exist until
its bytes do, and appears the instant they land — with no extra client call,
across every medium (including S3, where a client uploads straight to the
bucket and nothing else tells the API the bytes arrived).

It is deliberately small — one resource, `files` — because the point is the
handshake, not the business domain. On the happy path the bytes never pass
through a request handler: the API mints a capability, and the client talks to
the medium directly.

## The flow

```
1. POST /files                    -> 202  allocate a key + mint an upload capability
                                           (nothing persisted yet)
2. PUT/POST <capability>          -> 200  the client transfers the bytes directly
                                           to the medium (PUT for Local/SQL, a
                                           presigned POST for S3)
   -- the file now exists --
3. GET  /files/{id}                -> 200  read resolves directly against the medium
   GET  /files                    -> 200  search lists the medium's own objects
4. GET  /files/{id}/download       -> 200  a fresh `get` capability URL (JSON)
   GET  /files/{id}/content        -> 200  the bytes themselves (redirect for S3,
                                            streamed for Local / SQL)
5. DELETE /files/{id}              -> 204  removes the object -- no orphan row
```

`GET /files`, `GET /files/count`, `GET /files/batch-read`, `POST
/files/batch-edit` (create / delete only) are the ordinary generated actions.
There is **no `update`** — a file's bytes are immutable once uploaded;
"changing" one means delete the old id and create a new one.

## `download` vs `content`

Two distinct routes exist rather than one dual-mode route, because a plain
`<a href>` / `<img src>` cannot attach an `Authorization: Bearer <api-key>`
header — only a cookie rides along automatically:

* **`GET /files/{id}/download`** returns the JSON capability
  (`{"url", "method", "expires_at", "headers"}`) — the "give me something I can
  inspect / retry / hand to another process" route, and the only sane option
  for an S3-backed file from a non-browser client.
* **`GET /files/{id}/content`** returns the bytes: a `307` redirect to a fresh
  presigned `GET` for S3 (so `curl -L` / `<img src>` "just work" in one hop),
  or streamed directly for Local / SQL (a redirect to `/_files/{key}` would buy
  nothing — the request never leaves this process anyway).

This example ships the open (`OpenDependencyBuilder`) default, so `/content`
is unauthenticated here and a plain `<img src="/files/{id}/content">` works out
of the box. The moment this resource is secured with an
`AuthorizedDependencyBuilder` + `ApiKeyAuthenticator` (as examples 03-05 do), a
bare `<img src>` / `<a href>` request carries **no** credential and gets a
`401` — only a cookie-based authenticator (`CookieAuthenticator`) keeps
`/content` ergonomic from plain markup once the app requires auth.

## What the medium decides

The medium is a `FileStore`, selected from config with **no code change**
(`MEDIUM_CLASS`; unset -> `LocalFileStore`). It decides what the capability URL
*is* and how the upload is verified, and the client flow is identical across
media:

| Medium | Upload capability | Verification | Bytes served by |
| ------ | ------------------ | ------------- | --------------- |
| `S3FileStore` | S3's **native** presigned `POST` (policy conditions) | S3 itself enforces size / content-type atomically | S3 directly (no API endpoint) |
| `LocalFileStore` | a **framework-signed** JWE `PUT` capability | `verify_upload` checks size / checksum before an atomic `os.replace()` | this app's `/_files/{key}` `PUT` / `GET` |
| `SqlFileStore` | a **framework-signed** JWE `PUT` capability | `verify_upload` checks size / checksum before the row commits | this app's `/_files/{key}` `PUT` / `GET` |

A size / checksum mismatch is simply rejected (`409`) — there is no `failed`
status to track; the client retries against the same (still-valid) capability.
For S3 the capability points straight at the bucket, so `register_file_routes`
mounts **no** transfer endpoint at all. For the local / SQL media the
framework mints its own capability (an encrypted token bound to one `(key,
operation)` pair) served by the two `/_files/{key}` routes.

## Layout

```
06_filestore/
├── README.md                 # this file
├── pyproject.toml            # standalone -- resourcey from git (parent checkout in-repo)
├── alembic.ini                # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                       # APP_* config (committed); pass with --env-file
├── .gitignore
├── file_store_example/        # the importable app package
│   ├── app.py                 # manager + medium + manifest + `files` surface (uvicorn target)
│   ├── models.py               # re-exports the (optional) SQL-medium FileBlob schema
│   └── files.py                # build_files_resource: the `files` resource over the medium
├── migrations/
│   ├── env.py                 # Alembic env, diffs against the ORM metadata
│   └── versions/               # generated + reviewed revisions
└── tests/
    ├── test_smoke.py          # the flow over an in-process local medium (httpx ASGI)
    └── test_e2e.py             # full suite against the committed migration (SQL medium)
```

There is **no table at all** for the default `LocalFileStore` medium — this
example needs no database to run. The one table this example *can* have is the
optional `file_blobs` table (`resourcey.filestore.sql_file_store.FileBlob`),
used only when `.env` selects the SQL medium
(`MEDIUM_CLASS=...SqlFileStore`); `models.py` re-exports it (and its
declarative base) so Alembic and the tests have a single schema import. An app
that needs different byte-storage columns declares its own medium and reuses
`FileResource` / `register_file_routes` the same way.

## Run it

A **standalone** `uv` project. From within `06_filestore`:

```bash
uv sync
uv run uvicorn file_store_example.app:app --env-file .env --reload --port 8086
# -> Uvicorn running on http://127.0.0.1:8086
```

The default local medium needs no database, so `alembic upgrade head` is only
needed when `.env` selects the SQL medium (see below).

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

Allocate a key and mint an upload capability (declaring the size and MIME type
up front):

```bash
curl -s -X POST http://127.0.0.1:8086/files \
  -H "Content-Type: application/json" \
  -d '{"name":"notes.txt","content_type":"text/plain","size":5}'
# -> 202 {"id":"...","name":"notes.txt","content_type":"text/plain","size":5,
#         "checksum":null,"etag":null,"updated_at":"...",
#         "upload":{"url":".../_files/<key>?token=...","method":"PUT","expires_at":"...","headers":{}}}
```

Transfer the bytes against the capability from the create response — the file
does not exist until this lands:

```bash
FILE_ID=...                       # the id from the create response
URL=...                            # upload.url from the create response
curl -s -X PUT "$URL" -H "Content-Type: text/plain" --data-binary "hello"

curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8086/files/$FILE_ID
# -> 200   (404 before the upload)
```

Fetch it back — `download` mints a fresh JSON capability, `content` returns
the bytes directly:

```bash
DOWNLOAD=$(curl -s http://127.0.0.1:8086/files/$FILE_ID/download)
URL=$(echo "$DOWNLOAD" | python -c 'import sys,json;print(json.load(sys.stdin)["url"])')
curl -s "$URL"
# -> hello

curl -s http://127.0.0.1:8086/files/$FILE_ID/content
# -> hello   (one hop; no second request needed)
```

With the default local medium the capability URL is relative to the API host. For
a cross-origin client set `MEDIUM_SIGNED_URL_BASE_URL` (e.g.
`http://127.0.0.1:8086`) so the minted URL is absolute.

The rest of the standard surface:

```bash
curl -s http://127.0.0.1:8086/files                                  # search (no filter/sort surface)
curl -s http://127.0.0.1:8086/files/count                            # count
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8086/files/$FILE_ID
# -> 204   (also removes the stored object -- no orphan)
```

## Configuration

The framework reads the process-wide `APP` prefix and does **no** `.env` loading,
so every command passes `--env-file .env`.

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `APP_SQL_CONNECTIONS_0_NAME` | `main` | Connection name (first is the default; only used by the SQL medium). |
| `APP_SQL_CONNECTIONS_0_URL` | `sqlite+aiosqlite:///file_store_example.db` | Complete SQLAlchemy URL (driver in scheme). |
| `APP_ENCRYPTION_KEY_ID` | `dev` | Cursor / capability key id. |
| `APP_ENCRYPTION_KEY_VALUE` | *(dev default + warning when unset)* | Cursor / capability key value. |
| `MEDIUM_CLASS` | *(unset -> `LocalFileStore`)* | Dotted path of the `FileStore` subclass. |
| `MEDIUM_ROOT` | `./.resourcey_files` | Local medium's byte directory. |
| `MEDIUM_BUCKET` / `MEDIUM_REGION` / `MEDIUM_PREFIX` | — | S3 medium's bucket / region / key prefix. |
| `MEDIUM_CONNECTION_NAME` | `main` | SQL blob medium's connection. |
| `APP_UPLOAD_URL_TTL_SECONDS` | `900` | How long an upload capability stays valid. |
| `APP_DOWNLOAD_URL_TTL_SECONDS` | `900` | How long a `download` / `content` capability stays valid. |
| `APP_MAX_SIZE` | *(unset)* | Cap on a declared upload size, in bytes. |

### Switching media

**S3** (needs the extra — `uv add 'resourcey[s3]'` for a standalone copy):

```bash
MEDIUM_CLASS=resourcey.filestore.s3_file_store.S3FileStore
MEDIUM_BUCKET=my-bucket
MEDIUM_REGION=us-east-1
# optional: MEDIUM_ENDPOINT_URL (MinIO), MEDIUM_PREFIX, MEDIUM_ACCESS_KEY_ID / _SECRET_ACCESS_KEY
```

The flow is unchanged: step 2's capability is now an S3 presigned `POST` (a
multipart form, not a raw `PUT`) whose policy conditions — the exact key, a
`content-length-range` from the declared size, the content type — make S3
itself reject an upload that does not match. The API mounts no transfer
endpoint, and `read` / `search` resolve directly against S3's `HeadObject` /
`ListObjectsV2`.

**SQL blob table** (`file_blobs` on the app's own connection):

```bash
MEDIUM_CLASS=resourcey.filestore.sql_file_store.SqlFileStore
MEDIUM_CONNECTION_NAME=main
```

The blob table (`FileBlob`) is the SQL medium's single source of truth for an
object's bytes *and* its `name` / `content_type` / `checksum` / `etag` —
exactly as the local directory and the S3 bucket are for theirs. It is
**storage, not the `files` resource**: the actual `files` surface is served by
`FileResource` through the store's `put` / `head` / `list_objects` seam, never
through the table's own routes. The table can optionally also be exposed,
read-only, with its `data` column projected away (for admin / debugging) via
`resourcey.filestore.sql_file_store.sql_file_blob_view` — this example does not
register it. Create the table with
`create_blob_tables(session_factory)` (or your own Alembic revision against
`resourcey.filestore.sql_file_store.FileBlobBase`); this example's committed
migration covers exactly this table, since it is the only one needed when the
SQL medium is selected:

```bash
uv run --env-file .env alembic upgrade head
```

## Security notes

* A capability URL is a **bearer token**: anyone holding it can use it until it
  expires. Keep the TTLs short; treat the URL like a credential.
* It is bound to exactly one `(key, operation)` pair, so a `get` token cannot be
  replayed as a `put`, nor against another object's key.
* The local / SQL media verify a declared size / checksum **before** the bytes
  become visible (`verify_upload`); a mismatch is a `409`, not a corrupted
  file — the client simply retries. S3 enforces the same conditions natively
  via its presigned-POST policy.
* The local medium rejects any key that is empty, absolute, `~`, or contains
  `..`, and re-checks the resolved path stays under `root`; it also writes via
  a temp file + atomic `os.replace()` so a concurrent reader never observes a
  truncated file or metadata that disagrees with the bytes.
* `GET /files/{id}/content` is only safely embeddable in plain `<a href>` /
  `<img src>` markup when the app is unauthenticated (as this example is) or
  secured by a `CookieAuthenticator` — an API-key-secured app still 404s /
  401s a credential-less request to it, same as every other route.
* Minting is **authorized**: both `download` and `content` resolve the file
  through the resource's normal `DependencyBuilder` seam first (a `404` before
  any capability is minted or any bytes move), so to secure the app pass an
  `AuthorizedDependencyBuilder` as `dependency_builder=` (exactly as examples
  03-05 do). This example ships the open (`OpenDependencyBuilder`) default for
  clarity.

## Auto-generated REST surface

| Method | Path | Action |
| ------ | ---- | ------ |
| `POST` | `/files` | create (hand-written — `202`, not the generated `201`) |
| `GET` | `/files/{id}` | read |
| `DELETE` | `/files/{id}` | delete |
| `GET` | `/files` | search |
| `GET` | `/files/count` | count |
| `GET` | `/files/batch-read` | batch_read |
| `POST` | `/files/batch-edit` | batch_edit (create / delete only — no `update`) |

Plus `GET /files/{id}/download`, `GET /files/{id}/content`, and — for the local
/ SQL media only — the framework-signed transfer routes `PUT` / `GET`
`/_files/{key}` (hidden from the schema; S3 mints native URLs and never
reaches them).
