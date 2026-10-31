---
name: filestore
description: File bytes as a resource — the FileStore medium seam, FileResource/FileService, the hand-written files routes, the signed-URL capability, and the S3 / SQL / local mediums. Load when working on uploads, downloads, or file storage.
version: "2.0.0"
---

# The file store

`src/resourcey/filestore/` makes file **bytes** a first-class resource without
putting the bytes on a request path. There is **no metadata table**: "does the
medium have the bytes" is the only source of truth for a file's existence.

## `create` *is* the upload

The upload is a plain `multipart/form-data` body with one `file` part — the same
shape an HTML `<input type="file">` produces, so Swagger UI and a no-JS form both
work. The file exists the instant `POST /files` returns `201`; there is no
capability-and-transfer handshake for the client to complete, and no `pending`
→ `ready` state to track.

```
POST   /files                 multipart upload                 201
GET    /files/{id}            read, resolved against the medium 200
GET    /files                  search the medium's own objects   200
GET    /files/count            count_objects                      200
GET    /files/batch-read       repeated ?id=                      200
POST   /batch-edit             not reachable (see below)          n/a
GET    /files/{id}/download    a `get` capability (JSON)          200
GET    /files/{id}/content     the bytes themselves               200/307
DELETE /files/{id}             remove the object                  204
```

There is **no `update`** — a file's bytes are immutable once uploaded;
"changing" one means delete the old id and create a new one.

## `FileStore` — the medium seam

`FileStore` (a `DiscriminatedUnionMixin`, `kind` = class name) declares
`put` / `get` / `head` / `delete` / `list_objects` / `count_objects` (async) and
`presign_get` (sync: native SigV4 for S3, a local JWE mint for SQL / local —
neither blocks the event loop). There is no upload presign, because `create` *is*
the upload. A store is its own async context manager, entered through
`Manifest(managers=[store])`, so its client lifecycle ties to the app.

`StoredObject` is a `head` / `list` result (`key`, `size`, `name`,
`content_type`, `checksum`, `etag`, `updated_at`). `name` / `content_type` /
`checksum` are `None` from a listing call that cannot report them cheaply (S3's
`ListObjectsV2`); `head` always reports them.

Three mediums:

* **`LocalFileStore`** — the default (single instance, dev, tests). Bytes under
  `root`, MIME type in a `.meta` sidecar (the filesystem records none), ETag an
  MD5 of the bytes. Keys are opaque server-assigned hex, never client paths:
  `..`, an absolute path, `~`, and an empty key are rejected, and the resolved
  path is re-checked to live under `root`.
* **`SqlFileStore`** — bytes in a dedicated `file_blobs` table (no second
  system). The blob table is **storage, not a resource**: never registered and
  never DTO-derived, so bytes cannot leak through a read model.
  `sql_file_blob_view` is an optional, read-only, `data`-hiding `ResourceView`
  for admin / debugging. The app owns the schema (`create_blob_tables` for
  `create_all`, or Alembic against `FileBlobBase`).
* **`S3FileStore`** — the production medium: native SigV4 pre-signed URLs
  (`presign_*` is local, no network round trip) and client put/get/head/delete/
  list/count. `boto3` is imported **lazily** (an explicit `client=` is the
  escape hatch), so `filestore` imports without the extra; the optional
  dependency is `resourcey[s3]`. A single-`PUT` cap applies
  (`S3_MAX_PUT_BYTES`, 5 GiB); multipart is out of scope.

`FileStoreConfig` (a `BaseConfig`) selects the medium with no code change
through a `LazyField`: `APP_MEDIUM_CLASS` names a `FileStore` subclass, and its
own fields then parse under the `APP_MEDIUM_` prefix. Unset selects
`LocalFileStore`. `APP_DOWNLOAD_URL_TTL_SECONDS` (default 15 min) and
`APP_MAX_SIZE` are the limits; there is no upload TTL, because `create` is a
direct upload. The `*_seconds` spelling keeps the value env-parseable;
`download_url_ttl` exposes a `timedelta`.

## `FileResource` / `FileService`

`FileDTO` is the medium-native existence record: `id` (the medium's opaque key —
a string, not a UUID, so the `core` UUID convention does not generate it),
`name`, `content_type`, `size`, `checksum`, `etag`, `updated_at`, plus an
internal-only `content` field that carries the bytes from transport to service
for one `create` call and is never projected onto any REST shape.

* `create` writes the bytes and returns the record; `size` / `checksum` are
  computed from the bytes received, never a client declaration.
* `read` / `search` / `count` / `batch_read` resolve directly against the
  medium's `head` / `list_objects` / `count_objects`.
* `search` / `count` have **no filter or sort surface** — a medium's listing
  call cannot filter or sort by declared metadata — and `search` omits `name` /
  `content_type` / `checksum` for the same reason.
* `delete` accepts the shared `condition` signature for parity but **rejects** a
  non-`None` condition (`_reject_condition`): the medium-native record has no
  filterable metadata to evaluate one against.
* **No batch write.** `register_file_routes` must be the sole mounter of the
  `files` routes, so the resource is not in the `Manifest` and the app-level
  `POST /batch-edit` cannot address it; `apply_edits` still works when called
  directly (delete only). Bulk deletion means one `DELETE` per file (#179).

`FileResource` mixes in `DefaultCacheStrategyMixin`; `files` advertises writes,
so it resolves to `LastModifiedCacheStrategy` over `updated_at`.

## `register_file_routes`

```python
manifest = Manifest(resources=[], managers=[store])
app = create_app(manifest, dependency_builder=builder)
register_file_routes(app, store, resource=files, dependency_builder=builder)
```

Called **after** `create_app`. It is the **sole** place `files`'s routes are
mounted — it calls `register_routes` itself, so the resource must **not** also
appear in `Manifest(resources=...)` (that would register a second route set on
its own router, which runs first and shadows the hand-written ones). Only its
medium goes in `managers=`. Passing an already-registered resource raises
`ResourceyConfigError`.

It mounts the standard surface plus:

* `POST {resource}` — the multipart upload. `files` does not declare
  `Action.CREATE` (the generic JSON create body cannot carry a file), so there
  is no generated route to replace; authorization is unaffected, because
  `AuthorizedService.create()` checks the `Action.CREATE` policy regardless of
  what the resource declares. The body is read in 1 MiB chunks, so `max_size`
  aborts as soon as it is exceeded.
* `GET {resource}/{id}/download` — mints a `get` capability, the JSON shape
  (`{"url", "method", "expires_at", "headers"}`).
* `GET {resource}/{id}/content` — the bytes: a `307` redirect to a fresh
  presigned `GET` for S3, or streamed directly for Local / SQL (a redirect to
  `/_files/{key}` would buy nothing — the request never leaves the process).
  Both `download` and `content` gate on the resource's own `read` first, so a
  missing / forbidden file is a `404` before any capability is minted.
* `GET /_files/{key}?token=...` — the framework-signed transfer endpoint,
  mounted only for a `SignedFileStore` medium.

Why `download` and `content` are separate: a plain `<a href>` / `<img src>`
cannot attach an `Authorization` header. `download` is the "give me something I
can inspect / retry / hand to another process" route; `content` is the one a
browser can embed, and it is only ergonomic unauthenticated or behind a
**cookie** authenticator. Under an API-key builder, a bare `<img src>` carries
no credential and gets a `401` — that is expected, not a bug.

## The signed-URL capability

`signed_url.py` mints a JWE over `encryption` carrying the key and an `exp` —
**download-only**, since `create` *is* the upload and there is no separate
transfer step to sign for. `verify_signed_url` rejects (400) a malformed /
tampered token, one whose `exp` has passed (the codec does **not** enforce it),
and one missing its key claim.

The token is a **bearer capability**: short-lived and bound to exactly one key.
Comparing the token's key against the request path is the route's job, and it
does — a token minted for key A presented on key B is rejected.
`SignedFileStore` owns the mint / verify helpers over an injected
`EncryptionService`, so a concrete medium implements only the medium
operations.

## Spec and example

`specs/filestore.qnt` pins the medium-native model: `create` writes the bytes
and the file is immediately visible; keys are unique; a download capability is
bound to exactly one key; `delete` removes the object and a delete of an absent
key is a no-op.

`examples/06_filestore` is the runnable app, and its README walks the flow.
