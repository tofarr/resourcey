# AGENTS.md — rules for agents and contributors working in `resourcey`

This file is the persistent memory for this repository. Agents (human or AI)
must follow these rules when producing or reviewing code.

Domain-specific and activity-specific rules live in on-demand skills
(`.agents/skills/`). Invoke the relevant skill before starting work on that
area. The always-on rules below apply to all code in this repo.

## Stack & tooling

* Python ≥ 3.11, asyncio-first. Never use blocking I/O on the request path.
* Manage dependencies with `uv`. Never hand-edit `uv.lock`; use
  `uv add/remove/sync`.
* FastAPI for HTTP. Pydantic v2 for all request/response schemas.
* SQLAlchemy 2 async ORM + asyncpg. Alembic for migrations.
* Quint for formal specs; every behavioral change to a resource must be
  reflected in `specs/` and verified with `quint typecheck` / `quint test`.

## Release state

The project is **pre-release** (no tagged release, no external consumers).
Backward-compatibility shims are not required when changing the public API:
removed query params (e.g. a dropped `offset` after switching to cursor
pagination) do not need to be rejected or aliased — they may simply be
ignored. Optimize for a clean, minimal API surface over migration ergonomics
until the first release.

## Repo layout

* `src/resourcey/` — the framework.
* `examples/01_message_board`, `02_mongodb`, `03_api_key_auth` — standalone
  `uv` projects, each with its own `pyproject.toml`, `.venv`, and committed
  `.env`. They are excluded from the root ruff/mypy config and linted as
  standalone projects.
* `.vscode/launch.json` + `tasks.json` — debug configs for the examples. Each
  launches `uvicorn <app>:app` with `cwd` set to the example directory (so its
  `.env` applies) and `python` pointing at that example's `.venv`. Ports:
  8081 (01), 8082 (02), 8083 (03).

## Core design principles

* **Resource-oriented.** A resource is the central unit. One declaration
  drives Pydantic schemas, SQLAlchemy models, the REST service, migrations,
  and permission checks.
* **Progressive enhancement.** Each layer is opt-in and composable. The
  generated service can be replaced by a hand-written route that calls the
  repository, or by raw SQLAlchemy — nothing breaks when you drop a rung.
* **Escape hatches.** FastAPI, SQLAlchemy, and Alembic are first-class. Never
  hide them behind an abstraction that cannot be bypassed.

## On-demand skills

Invoke these via `invoke_skill(name="...")` when working in the relevant area:

* `pr-quality-checks` — lint/type/coverage gates to run before opening a PR.
* `rest-api-routes` — REST API naming, verbs, batch endpoints, error shapes.
* `testing` — hermetic test setup, transaction isolation, coverage rules.
* `quint-specs` — when to update specs and how to verify them.
* `resources` — resource metadata, service generation, field annotations.
* `migrations` — Alembic autogeneration from resource models.
* `auth-rbac` — users, groups, roles, per-action permission computation.
* `config` — env parser usage, `BaseConfig` semantics, and
  `DiscriminatedUnionMixin`.
* `pr-review-checklist` — checklist for agents reviewing PRs.

### `auth2` replaces `auth`

`src/resourcey/auth2/` is the successor to `src/resourcey/auth/` (issue #63)
and **must never import it**. New authentication work goes in `auth2`; the old
package is deleted once the replacement is complete. The first piece is
`auth2_api_key.py`: `ApiKeyDependencyBuilder` (a `DependencyBuilder`) accepts
any of a list of env-configured API keys and composes the check with each
resource's service dependency, with `api_key_dependency` reusable in any
router.

The next rung up is the stored API key: `auth2_api_key_resource.py` declares
`ApiKey` (UUID id, optional `name`, generated `key`, timestamps) and
`auth2_api_key_service.py` reveals a minted key exactly once. The key is
`creatable=False` / `updatable=False` / `readable=False`, so the generated
create, update, and read models omit it and — because the query surface is
derived from the read model — `?sort=key` and `?key__eq=` are rejected too.
`ApiKeyService.create` widens the read model with the minted value for the
`201` only; `specs/api_key.qnt` pins that contract. The column stores the key
in plaintext so a later lookup-based authenticator can match a presented key;
moving to a digest column is the hardening step when that lands.

### Storage backends and the shared paging base

Three backends implement the same action contract: `SqlResource`/`SqlService`,
`MongoResource`/`MongoService`, and `ListResource`/`ListService`. Storage-agnostic
paging/sort/cursor/cache logic lives in
`src/resourcey/resource/paged_service.py` (`PagedService`) — a new backend
subclasses it and implements only its data access, never a copy of the cursor
or sort-validation code.

`ListResource` is **read-only**: it narrows `actions` to
`read`/`search`/`count`/`batch_read` so no write route is ever mounted. It is
installed with the models it serves (`ListResource(models=[...])`) and the
wrapped Pydantic model *is* the read model — there is no schema generation, no
create/update model, and no columns. It is **defensive** by default: every
object it outputs is a deep copy of the stored object, so a caller cannot
mutate the served collection through a result. The list *is* the storage,
delivered through the same `open_storage`/`build_service` seam; there is no
table and no migration.

The manifest is declared with resource **instances**, not types:

```python
manifest = ResourceManifest(resources=(Thread(), Message()))
app = manifest.create_app()
```

Because instances carry their own configuration, a resource needing per-app
inputs is simply constructed with them — that is how a `ListResource` gets its
data (`ListResource(models=countries)`), and how a caller can override a hook
per instance.

### `v2/core` — the DTO / Resource / Service layer

`src/resourcey/v2/core/` is a new, deliberately minimal package (issue #75)
that runs **parallel to** the existing packages: it states the architecture in
terms of **DTO**, **Resource**, **Service**, plus a **Manifest**, and the
existing modules are migrated onto it later. Nothing existing is removed by it,
and it is not a refactor. It sits one rung above the `v2/util` bottom layer and
imports only `v2/util` (the `Missing` sentinel) among project packages.

Four files, no `__init__.py`:

* `dto.py` — `DTO` is a plain declaration class (not a Pydantic model). Fields
  carry ordinary Pydantic annotations plus a `DtoField` describing how each
  projects into the six REST shapes via six `in_*` flags; tag a field with
  `Annotated[T, DtoField(...)]` (the assignment form is a `mypy --strict`
  error). `DtoField` carries **operation-scoped** defaults
  (`default_for_create` / `default_factory_for_create` and
  `default_for_update` / `default_factory_for_update`) with precedence
  *client value → default for that operation → `MISSING`*; an omitted update
  field with no update default is left unchanged (PATCH), an
  `in_update_request=False` field always takes its update default. Optionality
  is never inferred from the annotation. `DTO.__init_subclass__` applies the
  `id`/timestamp conventions (`created_at` write-once, `updated_at` re-set on
  every update) and wraps every field `ann | Missing = MISSING`. The create
  request carries concrete defaults; the update request keeps the `MISSING`
  sentinel on the wire boundary, and the route converts request→DTO through the
  single sanctioned hop `request_to_dto` (`model_dump(exclude_unset=True)` +
  `model_validate`). `Missing` is a usable
  annotation type (core schema + serializes to `null`), unlike the legacy
  `resourcey.resource.missing.MISSING`. The six REST models are field-selection
  **projections** of the DTO, never hand-written. Both `DTO` and `DtoField`
  expose a free-form `metadata: dict[str, Any]` that `core` never reads: class
  metadata is inherited/merged down the MRO (keyword or body, and not a field),
  field metadata is per-field and excluded from `DtoField` equality/hash so it
  never churns the derived models.
* `id_field_name` — `DTO.id_field_name` is the identifier: `id` by default,
  selectable with the `id_field_name=` class keyword (or a body attribute) to
  make another declared field the identifier (a natural key). Validated at
  subclass creation — a name with no matching field raises `TypeError`. The
  identifier is always immutable (excluded from update requests). The
  conventional `id` is server-generated by the SQL backend and so also excluded
  from create requests; a custom identifier stays client-supplied on create.
* Generated logical defaults (issue #94, `specs/dto_defaults.qnt`) — several
  conventions run at subclass creation, all governed by one rule: **expressed
  intent wins**. A bare `id: UUID` (the *conventional* `id`, not a custom
  `id_field_name`, not a non-UUID id) gets an application-side `uuid4`
  `default_factory_for_create` — server-generated, never a database default, so
  no round trip is needed; a UUID natural key stays client-supplied. A field
  carrying an explicit `DtoField` (via `Annotated` or a class attribute) is
  honoured **verbatim**: the conventions change neither its flags nor its
  defaults, and the same intent-first rule makes a column-level
  `default=` / `server_default` in the SQL path win over a
  convention-generated factory (a backend-generated key gets no factory at
  all). Declaring both a value and a factory for the *same* operation
  (`default_for_create` + `default_factory_for_create`) raises at declaration
  time. Generated defaults never reach a response model (a response projects
  stored values); timestamps appear in every response shape and no request
  shape.
* `resource.py` — `Resource` is a **genuine ABC**: every method is abstract, so
  `v2/core` keeps no behaviour and a backend supplies the whole contract. The
  abstract surface is the DTO / REST-model getters (`get_dto_type`,
  `get_rest_models`, `get_id_field`), `get_resource_path`,
  `get_cache_strategy`, the action declaration (`get_supported_actions`, no
  `actions` property) and exposure (`get_exposed_resource`, whose declaration
  wins outright), the service seam (`get_service(ctx)` — **sync**, takes an
  optional call-scoped `MutableMapping`, returns a `Service` that is the async
  CM), registration (`on_register` / `get_manifest`), and the lifecycle
  (`__aenter__` / `__aexit__`). Core stays free of storage *and* transport: the
  per-request FastAPI dependency is built in `v2/http`, not here.
* `service.py` — `Service` is generic over the DTO, declares the eight actions,
  and *is* the async context manager; a call before `__aenter__` raises. It
  carries no storage: session-per-service and session-per-operation are both
  expressible and core privileges neither. The shared rule is *whoever opens
  the storage owns its commit and close; a resource that finds storage already
  in `ctx` reuses it and neither commits nor closes it*.
* `manifest.py` — `Manifest` owns the resource set and lifecycle, and asserts
  at construction that every `get_supported_actions()` names only real
  `Action` members (a typo would otherwise silently drop a route). Construction
  also calls `resource.on_register(self)` on every resource (declaration order),
  handing each a reference to the manifest; `Resource.on_register(manifest)` is
  **sync** (never a coroutine) and only retains the reference, which
  `Resource.get_manifest()` reads back (`None` until registered). Sibling
  resources are resolved *lazily, later* through that reference (e.g. to verify
  foreign keys) — never from inside `on_register`, since registration ordering
  is not a contract.

HTTP construction (`create_app`) is **not** part of `v2/core` — it belongs to
the transport layer.

### `v2/sql` — the SQLAlchemy backend (issues #78 / #89)

`src/resourcey/v2/sql/` is the SQL backend on top of `v2/core`, laid out like
`core` (flat files by role, no `__init__.py`). The workflow is **model-first**:
a developer defines the SQLAlchemy ORM model they already work with, and the
framework infers the DTO from it. SQLAlchemy is the schema of record, so
migrations and foreign-key relations stay SQLAlchemy's / Alembic's concern and
a developer can drop straight back to SQLAlchemy.

* `resource.py` — `SqlResource` (a `Resource` subclass) is handed the
  **SQLAlchemy model** it serves plus an async session maker **as a required
  constructor argument**. It infers the DTO (and hence the REST models) from
  the model via `sqlalchemy_2_dto`. There is **no** DTO-to-model generation and
  no declarative base to manage; `model` / `table` / `metadata` / `id_column`
  properties are the escape hatches back to SQLAlchemy.
* `service.py` — `SqlService` holds the call-scoped `ctx` and the session
  factory and implements the eight actions. `search` does keyset cursor
  pagination ordered by the identifier, or by a validated `sort` field (with
  the identifier as a stable tie-breaker) when one is requested; `filters` is
  pushed into the `WHERE` clause before the page is taken.
* `sqlalchemy_2_dto.py` — `sqlalchemy_2_dto(model)` infers a DTO declaration
  from an ORM model: column types map back to Python annotations, the primary
  key becomes `id_field_name`, nullability becomes `ann | None`, and
  client-side `default` / `onupdate` become the create / update defaults with
  `in_create_request=False`; a server-side default or autoincrement key drops
  from create with no default (the database supplies it), and a nullable column
  with no default gets `default_for_create=None`. A
  column may override the inferred projection by placing a `DtoField` in its
  `info` under the `dto_field` key (`DTO_FIELD_INFO_KEY`); otherwise the
  projection is inferred from the column's generation behaviour. **Plain
  columns only**: a FK column is a plain scalar field; `relationship()`s are
  not projected (a known limitation — nested projection is its own future
  issue).
* `cursor.py` — tamper-proof keyset cursor encode/decode plus the keyset
  `WHERE` predicate; the cursor is a JWE from `v2/encryption`.

There is no `v2/sql/migration.py`: with SQLAlchemy as the schema of record,
migrations are delegated to SQLAlchemy and Alembic rather than generated from
the framework's own in-memory models.

### `v2/encryption` — the migrated encryption service (issue #78)

`src/resourcey/v2/encryption/` mirrors v1's layout and holds the migrated
`EncryptionService` (`encrypt_value` / `decrypt_value`, the cursor path, plus
`create_jwe_token` / `decrypt_jwe_token`, the auth-token path) and the key
config (`EncryptionKeysConfig` / `EncryptionKeyConfig`, with the
`encryption_key` + `decryption_keys` rotation model and the `kid` header). It
is built from an **injected config object** — an instance is passed into the
`SqlResource` — so `v2` stays free of the env-parsing singleton; how a caller
obtains the config is the caller's concern. It sits in its own package (not
`core`, which stays crypto-free, and not `sql`, so a future `v2` auth can use
it without reaching into `sql`).

### `v2/http` — the transport layer (issue #87)

`src/resourcey/v2/http/` holds HTTP assembly as **free functions**, not methods
on `Manifest` (which stays a plain container) and with no lazy imports:

* `app.py` — `create_app(manifest, *, cors_origins=None,
  dependency_builder=None)` builds a fresh FastAPI whose lifespan is `async
  with manifest`, then mounts routes + error handlers + CORS;
  `add_to_app(manifest, app, *, prefix="/", dependency_builder=None)` mounts
  the same onto a user-owned app and **does not wire a lifespan** (Starlette
  has one lifespan slot, so the caller composes it). Both thread
  `dependency_builder` to `register_routes`; the keyword-only, default-`None`
  extension keeps every current call site unchanged.
* `dependency_builder.py` — the per-request service-dependency seam (issue
  #86). `DependencyBuilder` (a `DiscriminatedUnionMixin`, so a later config
  rung selects it by `kind`) declares
  `get_service_dependency(resource) -> Callable[..., object]`; it returns an
  ordinary FastAPI dependency whose author may declare any parameter FastAPI
  can wire (the `Request`, other `Depends(...)`, i.e. an auth dependency) — the
  same composition the `v1` builder used, so the seam covers authentication as
  well as authorization. `DefaultDependencyBuilder` is the default: it builds
  the resource's own service over the request-scoped `ctx` and yields it. The
  public `request_ctx(request)` helper owns the call-scoped mapping (the
  request-state key is `resourcey_ctx`), shared by every resource in one
  request. The builder lives here, not on the `Manifest`, because it is
  transport code (`Request` is annotated `starlette.requests.Request`) and
  `v2/core` must not import `v2/http`; the manifest stays a plain container.
  The seam is **authorization only** — it cannot add or remove routes (that is
  `get_exposed_resource()`'s sole call), and a restrictive posture returns a
  dependency that *denies* rather than nothing, keeping the route visible in
  OpenAPI. `register_routes` calls it **once** at registration and asserts the
  result is callable, so a misconfigured builder fails at startup, not per
  request.
* `routes.py` — `register_routes(app_or_router, resource, *, prefix="",
  tags=None, dependency_builder=None)` resolves the exposed resource once and
  registers one route per supported action, tagged with the exposed resource's
  class name, through the `_route` no-clobber escape hatch (a developer's route
  wins). The builder is resolved on the **exposed** resource, so a projection's
  wrapped service is the projection's. It also holds `register_error_handlers`,
  the projection helper, and the batch-edit item model.

Where the port differs from `v1`: `get_rest_models()` replaces the
create/update/read model getters, so each action maps explicitly to its shape
(`create` → `create_response`, so a one-time-reveal field survives); services
return DTO instances, so the response is **projected** onto the REST model
(dropping `MISSING`) in the transport; `v1`'s filter surface is back (issue
#79) and its sort surface is back too (issue #97), so search is `limit` +
`cursor` + `sort` / `desc` + the `<field>__<op>` filter params. Caching is back
(issue #92) — see the `v2/cache` section below. The service dependency is built
through the configured `DependencyBuilder` behind the one private helper
(`_service_dependency`). The error envelope maps only what
`v2` has now — `NotFoundError`→404, `IntegrityError`→409, `ServiceError`→500,
pydantic→422 (kept by FastAPI) — and #83 extends the same function.

### `v2/util/search_filter.py` and `v2/sql/filter_converter.py` — filtering (issue #79)

Filtering is two-level. `v2/util/search_filter.py` (the bottom layer) holds the
storage-agnostic core: `SearchFilter[T]` is a frozen, generic
`DiscriminatedUnionMixin` whose only method is `matches(value) -> bool`, with the
standard node types `AllFilter`, `NoMatchFilter`, `AndFilter`, `OrFilter`,
`NotFilter`, `AttrFilter` (two type params, `ObjT`/`ValT`, because it applies a
`SearchFilter[ValT]` to one attribute of an `ObjT`), and the value leaves
`EqFilter` / `GtFilter` / `GeFilter` / `LtFilter` / `LeFilter` / `ContainsFilter`.
Normalisation cannot live in `__init__` (a frozen model cannot return a different
type), so the identities — `NoMatch` annihilates an `And`, `All` is dropped,
nested nodes flatten, double negation unwraps — live in the factory functions
`and_` / `or_` / `not_` / `attr`. Children are tuples (a frozen model with a list
is unhashable), and `All` / `NoMatch` reuse the `Singleton` mixin. `Contains` is
pinned needle-in-haystack with case-insensitive strings. `BaseObjectFilter`
subclasses declare `<attribute>__<op>` fields and lower themselves to a standard
tree via `create_standard_filter()`, cached in a `PrivateAttr` (never a field, so
it stays out of `model_dump`, which the count ETag hashes). The derived query
surface helper `operators_for_annotation` fixes the op set by field type.

`v2/sql/filter_converter.py` is the only v2 file importing SQLAlchemy for
filtering. It consumes a *standard* tree (an object filter has already lowered),
dispatched through **three registries** — logical (`All`/`NoMatch`/`And`/`Or`/
`Not`, no column), attribute (`Attr`, which resolves a name to a column and binds
it), and operator (the value leaves, reachable only *with* a bound column;
`(ctx, column, value) -> ColumnElement[bool]`), with an import-time completeness
assert. Every operator registers a `(positive, negated)` pair; the negated form
is NULL-safe (`IS DISTINCT FROM` for equality, `col IS NULL OR <complement>`
otherwise) so it agrees with the in-memory `matches` complement on NULL rows,
where naive SQL `NOT` would drop them. `SqlFilterConverter` splits `resolve()`
(the only IO phase; empty today) from `apply()` (pure statement building). Pushdown
is all-or-nothing: an unconvertible node raises `UnsupportedFilterError` (a
`v2/core/errors.py` error, mapped by transport to `501 unsupported_filter`) unless
the resource sets `allow_filter_iteration = True`, which materialises matching ids
in memory — an unbounded scan behind a public `GET` is otherwise a DoS, and
silently skipping a filter could leak a permission scope. A frozen
`SqlFilterContext(columns, session=None)` carries the bound columns and the live
transaction into handlers.

The query surface: `Resource.get_filter_operators()` returns the derived surface
(each field's allowed ops) and `Resource.get_search_filter_type()` returns `None`
by default; `SqlResource` derives the surface from the read model so a field is
filterable exactly when it is readable (a wrapper that projects `secret` away
still rejects `?secret__eq=`). `v2/http/routes.py` synthesises one typed `Query`
param per `<attribute>__<op>` for OpenAPI, and `_resolve_filters` rejects any
unknown `field__op` key with `InvalidInputError` (400) — FastAPI silently ignores
unknown query params, so a typo must not be dropped. The count route's ETag hashes
the resolved filter too, so distinct filters get distinct validators.

`specs/filtering.qnt` is the spec for this and pins four laws (all in `make
specs` and CI): (1) `and_()`'s identity/annihilator normalisation preserves
`matches` for every value; (2) the NULL-safe negated operator agrees with the
in-memory complement on every row *and* naive SQL `NOT` provably disagrees (the
NULL row is the counterexample); (3) that NULL-safe complement generalises from
equality to the ordering operators (`IS NULL OR <complement>`); and (4) a
positive pushdown agrees with `matches` on every row — the "convertible ⇒ SQL
agrees" property the all-or-nothing policy rests on. The flat unrolling means
`Or`/`Not` normalisation is not expressed.

### `v2/util/sort_order.py` and `v2/sql/sort_converter.py` — sorting (issue #97)

Sorting mirrors filtering, one rung lower in surface. `v2/util/sort_order.py`
holds the storage-agnostic core: `SortOrder[T]` is a frozen, generic
`DiscriminatedUnionMixin` whose only method is `compare(a, b) -> CompareResult`
(`LESS` / `GREATER` / `SAME`), and the initial standard node is
`AttrSortOrder[ObjT, ValT]` (`attribute`, `descending`) — a single attribute,
ascending or descending, the `v1` surface. `compare` is the in-memory reference
semantics (an in-memory backend and the spec use it) and orders by one attribute
only; `None` sorts first so it is total on a nullable attribute. The identifier
tie-breaker that makes *paging* total is the backend's job (an `ORDER BY` with
the identifier appended and the matching keyset predicate), so it is not in the
node. Multi-attribute sort is a later rung — the union makes it additive.

`v2/sql/sort_converter.py` is the only v2 file importing SQLAlchemy for sorting.
A single registry keyed on the `SortOrder` type (extensible via
`register_sort_order`), resolved through a frozen `SqlSortContext(columns,
id_column)` carrying only the resource's **sortable** columns — the same security
gate as filtering, so a field projected away cannot be sorted on (its relative
order leaks the hidden value). `apply(stmt, sort_order)` returns
`stmt.order_by(<sort> asc/desc, id asc)`, always appending the identifier as a
tie-breaker, with an import-time completeness assert so a new node without a
handler fails at startup.

The keyset cursor grew to the `v1` shape: `encode_cursor(..., sort_field,
ascending, sort_key, id_value)` and `decode_cursor` returns the tuple, with
`sort_field=None` for the default identifier-ordered case.
`keyset_predicate(sort_column, id_column, cursor_key, cursor_id, ascending)`
builds `(sort_key, id) > (cursor_key, cursor_id)` for ascending (mirrored for
descending) and collapses to a single id comparison when the sort column *is*
the id column. `SqlService.search` validates the sort field, orders by it, and
**rejects** a cursor whose `(sort_field, ascending)` does not match the current
request (`InvalidInputError` → 400) rather than applying the key against the
wrong column; changing the sort of a paged request therefore means starting a
new search (no cursor), matching `v1`.

The sort surface: `Resource.get_sortable_fields() -> frozenset[str]` is derived
from the read model (readable ⇒ sortable) and `Resource.get_sort_order_type()`
returns `None` by default; `SqlResource` derives the surface from `read_response`
so `?sort=secret` is rejected. `v2/http/routes.py` exposes typed `sort` / `desc`
query params on search and passes them through; unknown or non-sortable fields
return 400. `count` is order-free and unaffected. Sorting changes the order of
the returned items and the ETag already hashes the ordered projection, so
distinct sorts get distinct validators with no extra wiring.

`specs/sorting.qnt` pins four laws (in `make specs` and CI): (1) `compare` is
antisymmetric and transitive and equal keys fall back to the identifier (a total
order); (2) the pushed-down keyset predicate keeps exactly the rows after the
cursor in `compare` order; (3) descending is the ascending mirror with the
identifier tie-breaker preserved; and (4) a cursor is accepted only under the
`(sort_field, ascending)` it was built for. The single-attribute model is
unrolled over a finite row universe, as `filtering.qnt` unrolls its tree.

### `v2/cache` — the migrated cache surface (issue #92)

`src/resourcey/v2/cache/` mirrors the old `resourcey.cache` layout (no
`__init__.py`):

* `cache_header.py` — `CacheHeader` (the `etag` / `updated_at` / `expire_at`
  value object and the `is_modified` matrix). HTTP-agnostic.
* `cache_strategy.py` — the concrete `CacheStrategy` base plus
  `ETagCacheStrategy` / `LastModifiedCacheStrategy` / `OptimisticCacheStrategy`.
  The concrete base is a `DiscriminatedUnionMixin` **and** extends
  `resourcey.v2.core.service.CacheStrategy` (the placeholder `v2/core` names),
  so a strategy satisfies the core-level seam while its behaviour lives in
  `cache`. `get_cache_header(items, *, context=...)` hashes the read-model
  projection; `count_cache_header(count, filters)` handles the bare-integer
  `count` route (a count-derived ETag; never last-modified).
* `cache_defaults.py` — `default_cache_strategy(rest_models)`: last-modified
  when the read model carries `updated_at`, else ETag.

The wiring: `Resource.get_cache_strategy()` returns `None` in `v2/core` (which
stays dependency-free); `SqlResource` overrides it to resolve the default
**per instance** (one `SqlResource` class serves many DTOs, so a class-level
cache would leak between them) and caches it on the instance. A developer
overrides the same hook to change the policy. `v2/http/routes.py` reads the
exposed resource's strategy once, passes it to every route builder, and each
handler emits `ETag` / `Last-Modified` / `Cache-Control` / `Expires` and
short-circuits a conditional `GET`/`HEAD` to `304 Not Modified`; a validator
with no freshness window forces revalidation with `Cache-Control: no-cache`.
Only the projected REST representation is hashed, so the ETag validates exactly
the bytes sent.

### `v2/` isolation

`v2/core`, `v2/sql`, `v2/encryption`, `v2/util`, `v2/config`, `v2/cache`, and
`v2/http` are **parallel** to the existing packages — nothing existing is
removed by them and they are not a refactor. The old `v1` packages/modules (and the old
`resourcey.encryption`) stay in place until a follow-up removal. A test asserts
that no module under `v2/` makes a **runtime** import of any `resourcey` code
*outside* `v2/` (a static AST walk covering every v2 layer in one rule),
`if TYPE_CHECKING:` imports still allowed. A second test pins the **layer
ranks** `util < core < {sql, http, config, cache, encryption}`: no module
imports a strictly-higher project layer at runtime. `v2/sql` implements
whatever small helpers it needs locally rather than reaching for
`resourcey.util`.

### `v2/util` and `v2/config` — the config rung

`src/resourcey/v2/util/` (issue #82) is the **bottom layer** of `v2` and where
the dependency-free vendored leaves now live: `models.py`
(`DiscriminatedUnionMixin`), `import_paths.py` (dotted-path resolution),
`env_parser.py`, and `missing.py` (the `Missing` / `MISSING` sentinel, moved
here by issue #86). They are copies, not moves — v1 `resourcey/util/` is
untouched until it is removed. `v2/util` imports **no project package** at all
(not even `v2/core`), so the layer ranks are a clean

    util < core < {sql, http, config, cache, encryption}

and `v2/core` may import `v2/util` — the dependency runs one way.

`src/resourcey/v2/util/singleton.py` (issue #95) is a second, non-vendored
leaf: a small `Singleton` mixin for the process-wide pieces the framework
keeps accruing (encryption service, dependency builders, caches). Constructing
a subclass twice returns the same instance, and each concrete class's `__init__`
runs **exactly once** on first construction, so the first construction wins —
later calls with other arguments do not reset it. Each concrete subclass caches
independently, and it composes with both Pydantic `BaseModel` and
`DiscriminatedUnionMixin` because it stores the instance and the initialized
flag on the class's own `__dict__` (never a field) and only guards a class's
*own* `__init__` — it never injects one, which would reroute pydantic's
validation. `clear_singleton_cache()` mirrors
`BaseConfig.clear_instance_cache()` and clears only the class it is called on.
It imports only the standard library.

**One sentinel.** `Missing` / `MISSING` from `v2/util/missing.py` is the only
definition in `v2`; `v2/util/env_parser.py`, `v2/core/dto.py`,
`v2/config/lazy_field.py`, and `v2/sql/service.py` import it and there is no
re-export. A test pins `env_parser.MISSING is missing.MISSING` so a future
re-copy of the vendored file cannot quietly reintroduce a second sentinel.

`src/resourcey/v2/config/` ships the generic machinery only: `config_base.py`,
`config_loader.py` (`load_dotenv`), and `lazy_field.py`. `FrameworkConfig`,
`DbConfig`, `MigrationConfig`, `AuthConfig`, `IdpConfig`, and
`DependencyBuilder` are deferred to a later PR, so there is no
`config_framework.py` / `config_dependency.py` here yet, and no
`config_runtime` at all.

`BaseConfig.get_instance()` caches **per class** and is typed to the owning
class: `MyAppConfig.get_instance()` returns a `MyAppConfig`,
`FrameworkConfig.get_instance()` returns a `FrameworkConfig`. There is no
super/subclass acceptance check and therefore no "not a subclass" error, and
`clear_instance_cache()` clears only the class it is called on — a base and a
subclass cache independently. The environment is the single source of truth:
each class parses its own slice under its own `get_prefix()`, so an app config
can extend a framework config without the framework needing to know the app's
fields. `ResourceyConfigError` (with `ResourceyError`) lives in
`v2/core/errors.py` and covers build/parse failures only; `ServiceError` /
`NotFoundError` stay in `v2/core/service.py`.

The `v2` isolation test is widened to cover **all** of `v2/`: no module under
`v2/` may make a runtime import of any `resourcey` code outside `v2/`, with no
exemption for the legacy `resourcey.util`. It also asserts the core file set is
exactly `{dto, errors, manifest, resource, service}.py` and that no `v2` module
imports `openhands`.

## Database configuration — one connection

`FrameworkConfig.database` is a single `DbConfig` (`url` + optional
`SecretStr` `password`); there is no separate `MongoConfig`. An app talks to
SQL **or** MongoDB, never both, so one connection field is enough. The URL
scheme selects the backend: `postgresql+asyncpg` / `sqlite+aiosqlite` (SQL),
`mongodb://` (Mongo via motor), or `embedded://<db>` (in-process mongomock).

The password is its own field, not part of the URL, so it can be injected from
a dedicated env var (`RESOURCEY_DATABASE_PASSWORD`) and encrypted at rest
(e.g. with SOPS) independently of the plaintext URL. `DbConfig.database_url`
splices it in for SQLAlchemy (percent-encoding reserved characters); for Mongo
the plaintext is passed as a `MongoClient` kwarg, and **only when set** —
`password=None` is not neutral to pymongo, it clears a URL-embedded password.
The Mongo database name is the URL's database component (`embedded://<db>`
takes it from the host), read from the string so multi-host URLs need no DNS.

## Code structure — reusable & testable

* Methods are short and single-purpose. If a method exceeds ~40 lines or does
  more than one thing, split it into named, individually testable helpers.
* Prefer pure functions for logic; isolate I/O at the edges.
* No business logic in route handlers — handlers validate, call a service, and
  serialize. Services contain logic; repositories contain data access.
* Layering: `routers → services → repositories → models`. Do not skip layers
  (a router must not query the DB directly).
* Shared behavior goes in a common module; do not copy-paste across resources.

### File & directory layout

* One flat directory per feature, directly under `src/resourcey/` (e.g.
  `user/`, `rbac/`). No `models/`/`routes/`/`services/` subfolders.
* Files inside a feature directory are flat and prefixed with the feature name
  for global uniqueness: `user_models.py`, `user_schemas.py`,
  `user_router.py`, `user_service.py`.
* No `__init__.py` unless it performs real package-level work. Default to
  namespace packages — convention over configuration.
* Genuinely shared, cross-cutting code lives in `src/resourcey/util/`,
  outside the per-feature pattern.

## Comments

* Concise but explicit. Describe only what is not obvious from reading the
  code.
* Do not restate the code, narrate changes, or describe nearby behavior.
* Valid uses: non-obvious invariants, workarounds, subtle ordering/locking,
  deliberate trade-offs.
* Docstrings: one-line summary for trivial functions; summary + args/returns
  only when types don't make it obvious.

### No `__all__` exports lists

Do not add `__all__` to modules. The codebase uses no wildcard imports
(`from x import *`), so an explicit exports list is pure repetition of the
names already defined at module scope. Keep the public API implicit: every
non-underscore-prefixed name is importable, and consumers import the names
they need directly.

## Vendored utilities

`resourcey.util.env_parser` and `resourcey.util.models` (including
`DiscriminatedUnionMixin`) are vendored from the OpenHands Software Agent SDK.
They must remain self-contained: **no `openhands` import may be introduced**.
When upgrading behaviour from upstream, copy the logic, do not add a
dependency.

## Issue tracking & roadmap

Work is tracked via [GitHub issues](https://github.com/tofarr/resourcey/issues).
Create an issue before starting non-trivial work; reference it in commits and
PRs.
