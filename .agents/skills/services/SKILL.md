---
name: services
description: The backend-agnostic core contract — DTO/DtoField/RestModels, the Resource ABC, Service and the eight actions, Manifest, conditional writes, caching, and encryption. Load before changing any behaviour every backend shares.
version: "2.0.0"
---

# The `core` contract

`src/resourcey/core/` is deliberately minimal: `dto.py`, `errors.py`,
`manifest.py`, `resource.py`, `service.py` — nothing else, no `__init__.py`.
It imports only `util` among project packages. HTTP assembly and storage live
in the layers above, so every behaviour here is shared by all backends.

## DTO — the one field-level declaration

`DTO` is a plain declaration class (not a Pydantic model). Each field carries an
ordinary annotation plus a `DtoField` describing how it projects into the six
REST shapes:

| flag | shape |
|---|---|
| `in_create_request` / `in_create_response` | `POST /{resource}` |
| `in_update_request` / `in_update_response` | `PATCH /{resource}/{id}` |
| `in_read_response` | `GET /{resource}/{id}` |
| `in_search_response` | search items, batch_read, apply_edits results |

Tag with `Annotated[T, DtoField(...)]`. The bare-assignment form is a
`mypy --strict` error.

`DtoField` also carries:

* **operation-scoped defaults** — `default_for_create` /
  `default_factory_for_create`, `default_for_update` /
  `default_factory_for_update`. Precedence is *client value → that operation's
  default → `MISSING`*. An omitted update field with no update default is left
  unchanged (PATCH); an `in_update_request=False` field always takes its update
  default. Optionality is never inferred from the annotation. Declaring a value
  and a factory for the same operation raises at declaration time.
* **`request_type`** — a narrower wire type for the create / update *request*
  models only, while the response models keep the declaration's own type. Use it
  when the accepted input set is smaller than the representable output set (a
  job's `status` accepts `PENDING` / `SCHEDULED` / `CANCELLED` but reads back
  `RUNNING` / `COMPLETED` / `ERROR`). `_UNSET` means no override.
* **`references`** — the *singular* resource name this field points at
  (`"thread"`, never `"threads"`). Tri-state: `_UNSET` = derive from an FK
  column's target table (SQL only), a string = use verbatim, `None` = suppress.
  `core` never reads it; the manifest uses it to fail fast.
* **`metadata`** — free-form, never read by `core`, excluded from equality so it
  never churns the derived models.

`DTO` conventions at subclass creation, all governed by **expressed intent
wins**:

* `id_field_name` selects the identifier (`id` by default) — validated, so a
  name with no matching field raises `TypeError`. The identifier is always
  immutable (excluded from update requests). The conventional `id` is also
  excluded from create requests (the SQL backend generates it); a custom
  identifier stays client-supplied on create.
* A bare `id: UUID` gets an application-side `uuid4`
  `default_factory_for_create`. A UUID natural key stays client-supplied.
* `created_at` is write-once; `updated_at` is re-set on every update.
* A field with an explicit `DtoField` is honoured **verbatim** — the conventions
  change neither its flags nor its defaults.
* Every field is wrapped `ann | Missing = MISSING`. `Missing` is a usable
  annotation (core schema + serializes to `null`).

`RestModels` holds the six derived models (`create_request`,
`create_response`, `update_request`, `update_response`, `read_response`,
`search_response`). They are field-selection projections of the DTO, never
hand-written. A `SecretStr` field gains the secret serializer / validator in
every generated shape (see *Secrets* below).

Two helpers do the sanctioned conversions: `request_to_dto(dto_model, payload)`
(`model_dump(exclude_unset=True)` + `model_validate`) and
`derive_dto(dto, field_overrides=..., name=...)` (used by `ResourceView` to
re-declare fields without re-running the conventions).

See `specs/dto_defaults.qnt`.

## Resource — a genuine ABC

`Resource[T, K]` is generic over the DTO and identifier type. Every method is
abstract, so `core` keeps no behaviour; a backend supplies the whole contract.
The surface:

```python
get_dto_type() -> type[DTO]
get_rest_models() -> RestModels
get_id_field() -> str
get_resource_path() -> str
get_cache_strategy() -> CacheStrategy | None
get_queryable_fields() -> frozenset[str]
get_filter_operators() -> dict[str, frozenset[str]]
get_search_filter_type() -> type[BaseObjectFilter] | None
get_sortable_fields() -> frozenset[str]
get_sort_order_type() -> type[SortOrder] | None
resolve_sort_order(sort, desc) -> SortOrder | None
get_supported_actions() -> frozenset[Action]
get_exposed_resource() -> Resource | None      # the declaration wins outright
async get_service(ctx: MutableMapping | None = None) -> Service[T, K]
on_register(manifest) -> None                  # sync, never a coroutine
get_manifest() -> Manifest | None
async __aenter__() / __aexit__(...)
```

`get_service` is **async** and returns a `Service` that *is* the async context
manager, so the call site is `async with await resource.get_service(ctx)`.
`on_register` only retains the manifest; siblings are resolved lazily later,
never from inside it (registration order is not a contract).

## Service — the actions

`Action` is a `StrEnum` whose lowercased member names match the method names:
`CREATE`, `READ`, `UPDATE`, `DELETE`, `SEARCH`, `COUNT`, `BATCH_READ`. There is
no batch **write** member: batching a write is a transport concern
(`POST /batch-edit`), not a per-resource action (#179).

`Service[T, K]` declares them and *is* the async CM; calling an action before
`__aenter__` raises `ServiceError`. It holds no storage — session-per-service
and session-per-operation are both expressible.

```python
async create(payload: T) -> T
async read(id: K) -> T
async update(payload: T, *, condition: SearchFilter | None = None) -> T | None
async delete(id: K, *, condition: SearchFilter | None = None) -> bool
async search(search_filter=None, sort_order=None, cursor=None,
             limit=DEFAULT_LIMIT) -> Page[T]
async count(search_filter=None) -> int
async batch_read(ids: list[K]) -> list[T | None]
async apply_edits(edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]
```

`apply_edits` is **not** an action — it is an internal helper the app-level
`POST /batch-edit` calls, and the one place a batch is applied. Every backend
implements it as a loop over its own singular `create` / `update` / `delete`, so
it adds no behaviour of its own; call it directly when you need a batch without
HTTP.

`Page` carries `items`, `limit`, `next_cursor` — no `total` (counting is the
separate `count` action). `DEFAULT_LIMIT = 20`.

The **shared ownership rule**: *whoever opens the storage owns its commit and
close; a resource that finds storage already in `ctx` reuses it and neither
commits nor closes it.* The ctx is keyed by `StorageKey` (build one with
`storage_key(backend, *parts)`), and the key names the **storage**, not the
request: a SQL service keys on its maker's bound engine (so two makers over
one engine still share a session, while two engines never do), a Mongo service
on its connection and collection, a list on its served list, a file service on
its store. Sharing is therefore per connection — two resources on one
connection share a session; two on different connections (or different
backends) never adopt each other's storage, even in one ctx. Every resource
exposes `async get_storage_key()` for an escape hatch to seed storage where a
service will look for it.

`normalize_actions(actions)` drops `batch_read` when `read` is absent.
`normalize_action(action)` reduces `COUNT`→`SEARCH`, `BATCH_READ`→`READ` for
permission purposes.

### Conditional writes

`update` / `delete` (and each `Update` / `Delete` node of an `apply_edits` batch) take
an optional **native** `SearchFilter` condition. The write applies only when the
row's pre-write value satisfies it; a miss is **indistinguishable from an absent
row** — `None` for update, `False` for delete — so existence does not leak.

The condition is a `SkipValidation` field (an already-built tree is not
re-validated; a wire `dict` is resolved by a `mode="before"` validator).
Authorization folds the policy filter into the same condition, so scope is
enforced by one atomic write rather than a read-then-write race.

A backend must **refuse** the in-memory iteration fallback for a condition even
when the resource opted into it: materialising ids and then writing opens a
TOCTOU window. An unconvertible condition raises `UnsupportedFilterError`.

See `specs/conditional_write.qnt`.

### Secrets

`Service.serialization_context()` returns `None` by default (redacting). A
service overrides it — e.g. `{"expose_secrets": True}` — for the one response
that should reveal a secret. The transport threads it through `model_dump` /
`model_validate`, so the wire body *and the ETag that hashes it* agree.

`util/secret_serialization.py` reads `encryption_service` (encrypt / decrypt at
the storage boundary) or `expose_secrets` (plaintext) from the pydantic context
and otherwise redacts; **encryption wins over exposure**. See
`specs/secret_serialization.qnt`.

## Manifest

```python
Manifest(resources, managers=())
```

* Asserts at construction that every `get_supported_actions()` names a real
  `Action` (a typo would silently drop a route).
* Calls `resource.on_register(self)` on every resource, in declaration order.
* `managers=` takes app-lifecycle async context managers (a
  `SqlSessionManager`, a `MongoClientManager`, a `FileStore`, a `Channel`, a
  `BackgroundTaskScheduler`, a `JobRunner`, a `TriggerRunner` owner). They are
  entered **before** the resources and exited **after** them, so a resource can
  still use a manager while shutting down. The slot is typed generically
  (`AbstractAsyncContextManager`) because `core` must not import `sql`.
* `__aenter__` validates field `references`, then enters managers, then
  resources; `__aexit__` reverses it. Entering twice raises.

Resources are declared as **instances**, not types — instances carry their own
configuration:

```python
manifest = Manifest(resources=(SqlResource(Thread), SqlResource(Message)))
app = create_app(manifest)
```

HTTP construction is **not** part of `core`; see the `transport` skill.

## Caching

`core` names the concept only: `CacheStrategy` is a placeholder with
`get_cache_header`, `count_cache_header`, and `should_read`, all defaulting to
"no policy". The concrete strategies live in `cache`:

* `CacheHeader` — `etag` / `updated_at` / `expire_at` / `private`, plus the
  `is_modified` matrix. `private` marks a caller-scoped freshness window.
* `ETagCacheStrategy` — a stable hash of the read-model projection.
* `LastModifiedCacheStrategy` — `max(updated_at)`.
* `OptimisticCacheStrategy` — freshness window only, no validators;
  `expire_in` must be > 0.
* `should_read(read_at, etag)` — the programmatic freshness question.
  Optimistic is a pure time check; Last-Modified / ETag always return `True`
  (they need the source).
* `CacheStore` / `InMemoryCacheStore` / `CacheEntry` — the medium a
  `CachedResource` holds copies in.
* `CachedResource(resource, *, store=None, private=False)` — the read-through
  wrapper. It serves `read` / `search` / `count` / `batch_read` from the store
  while fresh and evicts on every write. A caller-scoped response is **never**
  stored (the wrapper's `private=` or an `Owner`-scoped inner service), because
  sharing a principal-narrowed body across callers is an authorization leak.
* `DefaultCacheStrategyMixin` — the shared default `get_cache_strategy()`,
  cached **per instance** (one `SqlResource` class serves many DTOs, so a
  class-level cache would leak between them). Selection order: read-only →
  optimistic, `private`, 600 s; else last-modified when the read model has
  `updated_at`; else ETag. The read-only window is **always private** — the
  API-key header is not `Authorization`, so RFC 9111's authenticated-response
  protection does not apply.

`specs/cache_defaults.qnt` and `specs/cache_freshness.qnt` pin the selection and
the freshness decision.

## Encryption

`EncryptionService` (built from an injected config, not a singleton) offers
`encrypt_value` / `decrypt_value` (at-rest field encryption), the cursor codec,
and `create_jwe_token` / `decrypt_jwe_token` (auth tokens). `EncryptionKeysConfig`
parses `APP_ENCRYPTION_KEY_ID` / `_VALUE` and `APP_DECRYPTION_KEYS_<n>_*` for
rotation; an absent key degrades to a loud dev default (`changeme` + a warning),
while a partially specified key is a hard `ResourceyConfigError`.
`get_encryption_service()` / `clear_encryption_service_cache()` are the
process-wide edge.

## Errors

`ResourceyError` (in `core/errors.py`) is the base. `ResourceyConfigError`,
`InvalidInputError`, `UnsupportedFilterError`, `ConflictError` also live there —
storage-neutral, so a backend can raise them without the transport importing a
driver. `ServiceError` / `NotFoundError` / `ForbiddenError` live in
`core/service.py`, each deriving from `ResourceyError`, so one
`except ResourceyError` catches every framework failure.
