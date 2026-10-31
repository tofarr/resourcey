---
name: backends
description: The storage backends — SqlResource (model-first), MongoResource (DTO-first), the read-only ListResource, and the narrowing ResourceView — plus filter/sort conversion, keyset paging, and migration hooks. Load when defining or modifying a resource, a projection, or a backend.
version: "2.0.0"
---

# Storage backends

Three backends plus one wrapper. The action contract, paging, sort, filters, and
cache headers are identical across all of them — a new backend subclasses
`Resource` / `Service` and implements only its data access, never a copy of the
shared cursor or sort-validation code.

| Backend | Base | Storage | Workflow |
|---|---|---|---|
| SQL | `resourcey.sql.SqlResource` | SQLAlchemy 2 async table | **model-first** |
| Mongo | `resourcey.mongo.MongoResource` | async `motor` collection | **DTO-first** |
| List | `resourcey.list.ListResource` | in-process list | read-only, DTO projected from a Pydantic model |
| View | `resourcey.view.ResourceView` | wraps any of the above | narrowing projection |

## SQL — model-first

```python
SqlResource(
    Thread,                       # the SQLAlchemy model you already have
    *,
    session_factory=...,          # escape hatch, wins
    session_manager=..., name=...,# else the process-wide manager + connection
    path=None,                    # default: pluralized kebab DTO name
    encryption_service=None,
)
```

SQLAlchemy is the schema of record. The DTO is inferred from the model by
`sqlalchemy_2_dto`: column types map back to annotations, the primary key
becomes `id_field_name`, nullability becomes `ann | None`, client-side
`default` / `onupdate` become the create / update defaults with
`in_create_request=False`, and a server-side default or autoincrement key drops
from create with no default. A nullable column with no default gets
`default_for_create=None`. A column may override the inference by placing a
`DtoField` in its `info` under `DTO_FIELD_INFO_KEY`.

**Plain columns only.** A FK column is a plain scalar field; `relationship()`s
are not projected (a known limitation — nested projection is its own future
issue). A column typed with `ModelType` keeps the bound model as the field's
annotation, so the request models carry that model's own schema (for a
`DiscriminatedUnionMixin`, the discriminated union of its kinds).

`ModelType(model, *, impl=JSON)` is a `TypeDecorator`: binding serializes with
`model_dump(mode="json")`, reading validates back. `impl` defaults to `JSON`, so
an existing `JSON` column adopts it with no migration. It gives a model-typed
field **no** filter / sort operators.

`SqlResource` exposes `model` / `table` / `metadata` / `id_column` /
`get_column_name` as escape hatches. There is no DTO→model generation and no
declarative base to manage.

`SqlService` implements the eight actions. `search` does keyset cursor
pagination ordered by the identifier, or by a validated sort field with the
identifier as a stable tie-breaker. `_apply_filters` pushes the filter into the
`WHERE`; `_apply_condition` pushes a write's condition there too and **refuses**
the in-memory fallback (see the `services` skill). `apply_edits` refuses a node
whose action the resource does not declare.

## Mongo — DTO-first

```python
MongoResource(
    ThreadDTO,
    *,
    client=None, database_name=..., collection_name=...,  # escape hatch
    client_manager=..., name=...,                          # else process-wide
    path=None, encryption_service=None,
)
```

Mongo has no schema of record, so the DTO drives everything: the six REST
models, the identifier, and the query surface. A bare `id: UUID` gets a
server-side `uuid4` create default from the `core` conventions.
`migrate_document(doc) -> doc` is the manual migration-on-read hook (default
no-op) invoked on every read path; `get_indexes()` / `ensure_indexes()` are
opt-in and run from `__aenter__`.

`MongoService` works against a duck-typed async collection (`insert_one`,
`find_one`, `find`, `find_one_and_update`, `delete_one`, `count_documents`,
`create_index`) so `mongomock` substitutes for `motor`. A UUID is stored as its
string under `_id`. A duplicate key becomes `ConflictError` — the
storage-neutral 409 — so the transport needs no driver import. A write's
condition is `$and`-merged with the `_id` match by `_write_query`, and the
in-memory fallback is refused there for the same TOCTOU reason as SQL.

`MongoClientManager` mirrors `SqlSessionManager`. `MongoConfig` parses
`APP_MONGO_CONNECTIONS_<n>_NAME` / `_URL` / `_PASSWORD` (field
`mongo_connections`, with `connections` as a read-only alias, because the env
parser derives the variable from the field name). `embedded://<db>` (and the
bare `embedded` marker) builds the in-process `AsyncEmbeddedClient` over
`mongomock`; `mongodb://` builds a real `motor` client with the password passed
as a client kwarg **only when set** (`password=None` would clear a
URL-embedded password).

`motor` is required only by the `mongodb` extra: `mongo` is import-safe without
it, and building a real client without it fails with an actionable
`ImportError` naming `resourcey[mongodb]`. `mongomock` / `pymongo` are
dev-only.

## List — read-only

```python
ListResource(models, *, model=None, dto=None, path=None, defensive=True)
```

For in-process reference data (country codes, feature flags, catalog entries)
served over the same REST surface with no table and no migration. The served
Pydantic model is projected onto a DTO via `pydantic_2_dto`; an explicit `dto=`
wins. It narrows `get_supported_actions()` to exactly
`{read, search, count, batch_read}`, so **no write route is mounted** — a write
is a `405`, not an unimplemented handler.

`defensive=True` (the default) deep-copies every output, so a caller cannot
mutate the served collection through a result. `ListService` mirrors the SQL
`keyset_predicate` in memory (NULLs first ascending / last descending) and
rejects a cursor reused under a different `(sort_field, ascending)`.

## View — the narrowing wrapper

```python
ResourceView(
    resource=secrets,
    exposed_field_overrides={"value": {"in_read_response": False, ...}},
    exposed_request_type_overrides={"status": CLIENT_STATUS_TYPE},
    exposed_actions=frozenset(Action) - {Action.UPDATE},
    path=None,
    cache_strategy=None,
)
```

The general-purpose least-privilege tool: declare a resource once with its full
storage truth, then narrow the public surface without touching the storage
class. "Delegate everything except `get_exposed_resource`" is *almost* right —
field overrides force the whole exposed surface to be recomputed:

* **Field projection** re-derives the DTO via `derive_dto`, so the `DTO`
  conventions do not re-run (a bare `id` keeps its `uuid4` factory). An unknown
  field, an override of the identifier, or an override that turns an
  inner-`False` flag back on is rejected at construction.
* **Query / sort surface** is recomputed from the view's read model.
  Load-bearing: delegating it to the inner would let `?secret__eq=` /
  `?sort=secret` pass the transport's validation and reach the inner service,
  leaking the hidden value or its relative order. Hiding fields while the inner
  declares an object filter is refused at construction.
* **Cache policy** is recomputed when the view narrows, so a view that becomes
  read-only selects the optimistic, `private` strategy rather than inheriting a
  writable inner's shared-cacheable validator. `cache_strategy=` is the escape
  hatch.
* **Actions** are normalized and must be a subset of the inner's: a view
  **narrows**, never widens.

`ViewService` re-asserts the view's actions on `apply_edits` — defense in depth,
since the inner service consults the inner (wider) action set. `__aenter__` /
`__aexit__` / `on_register` delegate to the inner so its lifecycle still runs.
Register the **view**, not the inner (registering both double-enters the inner
and mounts duplicate routes).

See `specs/exposure.qnt`.

## Filtering and sorting

Both are two-level. The storage-agnostic half is in `util`:

* `SearchFilter[T]` — a frozen `DiscriminatedUnionMixin` with `matches(value)`.
  Nodes: `AllFilter`, `NoMatchFilter`, `AndFilter`, `OrFilter`, `NotFilter`,
  `AttrFilter[ObjT, ValT]`, and the leaves `EqFilter` / `GtFilter` / `GeFilter`
  / `LtFilter` / `LeFilter` / `ContainsFilter` / `InFilter`. Normalisation
  (NoMatch annihilates an `And`, All is dropped, nesting flattens, double
  negation unwraps) lives in the factories `and_` / `or_` / `not_` / `attr`,
  not in `__init__` (a frozen model cannot return a different type). Children
  are tuples so the tree stays hashable. `InFilter.values` is capped at
  `MAX_IN_VALUES = 500`.
* `SortOrder[T]` — a frozen `DiscriminatedUnionMixin` with
  `compare(a, b) -> CompareResult`. The one standard node is
  `AttrSortOrder(attribute, descending)`. `None` sorts first so the order is
  total on a nullable attribute. The identifier tie-breaker that makes *paging*
  total is the backend's job, not the node's.
* `BaseObjectFilter` — declares `<attribute>__<op>` fields and lowers to a
  standard tree via `create_standard_filter()`, cached in a `PrivateAttr`.
* `operators_for_annotation(annotation)` — the derived op set by field type:
  bool → `eq`/`in`; containers and Pydantic models → none; orderable types
  (int/float/Decimal/datetime/date/time) → `eq`/`in`/`gt`/`ge`/`lt`/`le`;
  strings and UUID → those plus `contains`; anything else → `eq`/`in`.

The SQL half (`sql/filter_converter.py`, `sql/sort_converter.py`) and the Mongo
half (`mongo_filter_converter.py`, `mongo_sort_converter.py`) are the only
framework files importing their driver for these concerns. Each dispatches
through **three registries** — logical / attribute / operator — with an
import-time completeness assert. Every operator registers a `(positive,
negated)` pair; the negated form is NULL-safe (`IS DISTINCT FROM` for equality,
`col IS NULL OR <complement>` otherwise) so it agrees with the in-memory
`matches` complement on NULL rows, where naive SQL `NOT` would drop them. Mongo
reproduces the same complement on absent / null fields and uses
`{"$nor": [{}]}` as its match-nothing sentinel.

Pushdown is **all-or-nothing**: an unconvertible node raises
`UnsupportedFilterError` unless the resource sets `allow_filter_iteration =
True`, which materialises matching ids in memory. An unbounded scan behind a
public `GET` is otherwise a DoS, and silently skipping a filter could leak a
permission scope.

The security gate is the same everywhere: the filter and sort surfaces derive
from `read_response`, so a field projected away is neither filterable nor
sortable.

See `specs/filtering.qnt` and `specs/sorting.qnt`.

## Keyset paging

`encode_cursor(..., sort_field, ascending, sort_key, id_value)` /
`decode_cursor` live in `util/cursor.py` — the tamper-proof JWE codec shared by
every backend. `keyset_predicate` stays in `sql/cursor.py` because it imports
SQLAlchemy, and that module re-exports the codec so existing importers keep
working; Mongo builds its own query.

The predicate is `(sort_key, id) > (cursor_key, cursor_id)` ascending and the
mirrored `<` descending — but it mirrors **only** the sort-key comparison, never
the identifier tie-breaker (`ORDER BY key DESC, id ASC`), so equal keys step to
a *greater* id instead of skipping the row. A `None` cursor key uses explicit
NULL tests (NULLs sort first ascending, last descending). The predicate
collapses to a single id comparison when the sort column *is* the id column.

A cursor is accepted only under the `(sort_field, ascending)` it was built for;
a mismatch is `InvalidInputError` (400) rather than applying the key against the
wrong column. A malformed or tampered cursor is also 400, never 500.

## Migrations

There is no `resourcey migrate` wrapper and no `sql/migration.py`: SQLAlchemy is
the schema of record, so Alembic diffs against the app's `Base.metadata` (or a
framework base such as `ApiKeyBase` / `RbacBase` / `JobsBase` / `FileBlobBase`).
See the `migrations` skill.
