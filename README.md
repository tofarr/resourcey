# resourcey

A resource-oriented Python web framework built on FastAPI, SQLAlchemy, and
Alembic — **progressive enhancement with escape hatches to the underlying
stack.**

## Why?

Most web frameworks either hide the database entirely or make you wire every
layer by hand. `resourcey` takes a middle path: you declare a **resource**
with enough metadata to describe how it should be stored, validated, and
exposed, and the framework generates a service for you. When the abstraction
gets in the way, you drop back a rung — to FastAPI, SQLAlchemy, or Alembic —
and nothing breaks.

The guiding principles:

* **Resource-oriented.** A resource is the central unit. Define it once with
  rich metadata; derive Pydantic schemas, SQLAlchemy models, a REST service,
  migrations, and permission checks from that single declaration.
* **Progressive enhancement.** Each layer is opt-in. Use the generated
  service, or write your own route that calls the repository directly, or
  query SQLAlchemy yourself. The layers compose; they don't trap you.
* **Escape hatches.** FastAPI, SQLAlchemy, and Alembic are first-class
  citizens, not implementation details. You can always reach them.

## What a resource gives you

Declaring a resource produces:

* **Pydantic models** for request/response validation, derived from the
  resource's field metadata.
* **SQLAlchemy models** for persistence, derived from the same metadata.
* **A service** exposing the standard actions over REST:
  `create`, `read`, `update`, `delete`, `search`, `count`, `batch_read`, `batch_edit`.
* **Search filtering** — `search` and `count` accept `<field>__<op>` query
  params (`?title__contains=ali&id__gt=5`). A field is filterable exactly when
  the read model exposes it, so a hidden field is rejected `400`; a resource can
  declare a richer filter via `get_search_filter_type`. Filters lower to a
  storage-agnostic tree and are pushed into the SQL `WHERE` clause; an
  unconvertible filter fails loudly unless the resource opts into an in-memory
  fallback.
* **Migrations** — Alembic autogeneration from the current models (driven
  directly against the SQLAlchemy metadata), so you can derive schema changes
  from your resource declarations.
* **Authorization** — pluggable `Policy` rules reduce a principal to a
  per-action filter: API-key / cookie authentication, per-app roles, and a
  stored users / groups / roles RBAC store.

## Storage backends

A resource is declared against one of three backends; the action contract,
paging, sort, filters, and cache headers are identical across all three.

| Backend | Base class | Storage |
|---|---|---|
| SQL | `resourcey.sql.SqlResource` | SQLAlchemy 2 (async) table |
| Mongo | `resourcey.mongo.MongoResource` | an async `motor` collection |
| List | `resourcey.list.ListResource` | an in-process list of Pydantic objects |

A **list-backed** resource is read-only: it narrows its actions to
`read` / `search` / `count` / `batch_read` and serves data already modelled as
Pydantic objects (country codes, feature flags, catalog entries) without
copying it into a table. Because the objects already exist as Pydantic models,
there is no schema generation — the model is projected onto the DTO the REST
shapes derive from, and (being read-only) there is no create/update model and
no columns. The list *is* the storage, so there is no table and no migration.

```python
from pydantic import BaseModel
from resourcey.list.list_resource import ListResource


class Country(BaseModel):
    id: str
    name: str
    iso3: str


countries = [
    Country(id="us", name="United States", iso3="USA"),
    Country(id="ca", name="Canada", iso3="CAN"),
]
resource = ListResource(countries, path="countries")

manifest = Manifest(resources=(resource,))
```

`defensive=True` is the default: every object the resource outputs is a deep
copy of the stored object, so a caller cannot mutate the served collection
through a result. Pass `defensive=False` to serve the stored objects directly.

## The `core` package — DTO, Resource, Service, Manifest

`src/resourcey/core/` is a deliberately minimal package that states the
architecture in terms of three constructs plus a manifest. It is the foundation
every backend and transport layer is built on.

The split separates **the DTO** (the data object you work with internally) from
**the six REST models** (the wire shapes), which the older code entangled, and
does away with six hand-written models per resource — those are now *derived*.

```python
from resourcey.core.dto import DTO
from resourcey.core.manifest import Manifest
from resourcey.core.resource import SqlResource


class Thread(DTO):
    id: int
    title: str


class Message(DTO):
    id: int
    thread_id: int
    body: str


manifest = Manifest(resources=[SqlResource(Thread), SqlResource(Message)])
```

The four files:

* **`dto.py`** — a `DTO` is a plain declaration class (not a Pydantic model).
  Fields carry ordinary Pydantic annotations plus a `DtoField` describing how
  each projects into the six REST shapes via six `in_*` flags
  (`in_create_request`, `in_create_response`, `in_update_request`,
  `in_update_response`, `in_read_response`, `in_search_response` — all default
  `True`, superseding the older `creatable` / `updatable` / `readable` triple).
  Tag a field with `Annotated[T, DtoField(...)]` — the canonical Pydantic
  mechanism; it keeps the real field type (the assignment form
  `key: str = DtoField(...)` is a type error under `mypy --strict`), and no
  explicit `| Missing` is needed because the generator widens every field
  itself:

  ```python
  class MyStoredKey(DTO):
      id: Annotated[
          UUID,
          DtoField(
              in_create_request=False, in_update_request=False, default_factory_for_create=uuid4
          ),
      ]
      key: Annotated[str, DtoField(in_read_response=False)]
      description: Annotated[str | None, DtoField(default_for_create=None)]
      created_at: Annotated[
          datetime,
          DtoField(
              in_create_request=False, in_update_request=False, default_factory_for_create=utc_now
          ),
      ]
      updated_at: Annotated[
          datetime,
          DtoField(
              in_create_request=False,
              in_update_request=False,
              default_factory_for_create=utc_now,
              default_factory_for_update=utc_now,
          ),
      ]
  ```

  `DtoField` carries **operation-scoped** defaults (`default_for_create` /
  `default_factory_for_create` and `default_for_update` /
  `default_factory_for_update`), whose precedence is *client value → default
  for that operation → `MISSING`*. Create and update do not want the same
  default: an omitted update field with no update default is *left unchanged*
  (PATCH semantics), while an `in_update_request=False` field is always omitted
  and so always takes its update default (the "always overwrite" case — how
  `updated_at` is re-set on every update while `created_at` is written once).
  Optionality is never inferred from the annotation: a nullable field with no
  create default is required on create. `DTO.__init_subclass__` applies the
  `id` / timestamp conventions and wraps every field as `ann | Missing`
  defaulting to `MISSING`, so an omitted field is distinguishable from an
  explicit `None`. The create request carries concrete defaults; the update
  request keeps the `MISSING` sentinel on the wire boundary so an omitted field
  is distinguishable from an explicit `null`, and the route converts a request
  into a DTO through the single sanctioned hop (`request_to_dto`, i.e.
  `model_dump(exclude_unset=True)` + `model_validate`).
  `Missing` is a usable annotation type (it carries a Pydantic core schema and
  serializes to `null`), so `UUID | Missing` validates. The six REST models are
  field-selection projections of the DTO — never hand-written — and the
  one-time-reveal case (`key` in the create response only) is expressible.
  Both `DTO` and `DtoField` carry a free-form `metadata: dict[str, Any]`, a
  general-purpose store for extra data that `core` never reads. A DTO's
  metadata is inherited and merged down the MRO and is not a field; a
  `DtoField`'s metadata is per-field and excluded from equality/hash, so
  differing metadata never churns the derived models.
  `DTO.id_field_name` selects the identifier — `id` by default, overridable
  with the `id_field_name=` class keyword to make another declared field the
  identifier (a natural key). It is validated at declaration time: a name with
  no matching field raises `TypeError`. The identifier is always immutable
  (never in an update request); the conventional `id` is also server-generated
  in the SQL backend and so excluded from create requests, while a custom
  identifier stays client-supplied on create, e.g.
  `class Country(DTO, id_field_name="code")` with a `code: str` field.

* **`resource.py`** — a `Resource` is derived from a DTO. `SqlResource` is the
  first backend (a DTO-derived table over an injected async session factory or
  a named connection from `SqlConfig`; the backend is chosen explicitly at
  construction, not inferred from config). `get_service(ctx)` is **async** and
  takes an optional call-scoped `MutableMapping`; awaiting it builds the
  service (a backend may resolve a connection first) and the returned `Service`
  is the async context manager that owns the storage, so the call site is
  `async with await resource.get_service(ctx)`. `get_supported_actions()` is the
  single action declaration (there is no `actions` property), and
  `get_exposed_resource()` composes on top of it — the exposed resource's
  declaration wins outright.
* **`service.py`** — `Service` is generic over the DTO, declares the eight
  actions, and *is* the async context manager; a call before `__aenter__`
  raises clearly. `Action` is the action enum.
* **`manifest.py`** — the `Manifest` owns the resource set and its lifecycle
  (it may also own app-lifecycle managers, e.g. a `SqlSessionManager`, entered
  before the resources and exited after them),
  and asserts at construction that every resource's supported actions name only
  real `Action` members (a typo would otherwise silently drop a route).

Storage ownership follows one rule, letting session-per-service and
session-per-operation both live:

> Whoever opens the storage owns its commit and close. A resource that finds
> storage already in `ctx` reuses it and neither commits nor closes it.

`ctx` is a plain `MutableMapping` keyed by module-level sentinels, so a caller
can pre-seed storage (the escape hatch) and every resource in the call adopts
it. `AppContext` (app-scoped) stays a separate concept. HTTP construction
(`create_app`) is deliberately **not** part of `core` — it belongs to the
transport layer, where the per-request service dependency is built through a
configurable `DependencyBuilder` (its default opens the resource's own service
over the request-scoped `ctx`). `util` is the bottom layer: an isolation test
pins the layer ranks `util < core < {sql, mongo, list, view, filestore, http,
config, cache, encryption, auth, tasks, triggers}`, so no module imports a
higher layer at runtime.

### Configuration — env-driven edges

`core` stays config-free (a service is built from the objects it is handed),
while the two env-driven *edges* — SQL connections and encryption keys — are
`BaseConfig` blocks that live with what they configure: `SqlConfig` in
`sql/`, `EncryptionKeysConfig` in `encryption/`. All read the one
process-wide prefix, `APP` by default, so connection `n` is
`APP_SQL_CONNECTIONS_<n>_NAME` / `_URL` / `_PASSWORD` and the key is
`APP_ENCRYPTION_KEY_ID` / `_VALUE` (plus `APP_DECRYPTION_KEYS_<n>_*` for
rotation). An absent encryption key degrades to a loud dev default
(`changeme` plus a warning); a partially specified one is a hard
`ResourceyConfigError`.

An app composes the blocks by inheritance and calls the composed config once at
its entry point:

```python
from resourcey.encryption.encryption_config import EncryptionKeysConfig
from resourcey.sql.sql_config import SqlConfig


class AppConfig(SqlConfig, EncryptionKeysConfig):
    """The app's single config object, composed from the framework blocks."""


config = AppConfig.get_instance()  # reads APP_* from os.environ
print(config.generate_env_template())  # a commented .env skeleton, secrets redacted
```

`get_instance()` caches **per class**, so `AppConfig.get_instance()` and
`SqlConfig.get_instance()` are distinct instances whose values agree (both read
the same env namespace). Framework internals resolve their own block;
`SqlResource` uses `get_encryption_service()` / `get_sql_session_manager()` by
default and accepts an explicit `encryption_service=` / `session_factory=` to
override. The framework does no `.env` loading — use `uvicorn --env-file` or a
wrapper script to populate the environment.

## Stack

| Concern | Tool |
|---|---|
| HTTP | FastAPI |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2 (async) |
| Migrations | Alembic |
| Package management | uv |
| Formal specs | Quint |
| Tests | pytest (≥90% coverage enforced) |

## Authorization (users, groups, roles)

`resourcey.auth` secures a resource through two composable seams: an
`Authenticator` (API-key and cookie authenticators) that produces a
`Principal`, and a `PolicyResolver` that maps that principal to `Policy` rules.
The built-ins are `AllowAll` / `DenyAll` / `ReadOnly` / `Owner`, with a
per-app role vocabulary (`RolePolicyResolver`) and a store-backed RBAC resolver
(`RbacPolicyResolver`) over `users` / `groups` / `roles` / `role_permissions` /
`resource_acls` tables. An `AuthorizedService` enforces the reduced filter per
action: a denied create is `403`, an out-of-scope read/update/delete is `404`,
and a denied search/count yields an empty page / `0`.

## Configuration

A typed environment-variable parser is bundled in `resourcey.util.env_parser`
(vendored from the
[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk/blob/28e8ed273617992e9556410804f54937cc059878/openhands-agent-server/openhands/agent_server/env_parser.py),
written by the same author). It supports complex nested types and polymorphism
that `pydantic-settings` cannot express. There is **no runtime dependency** on
the SDK.

A `DiscriminatedUnionMixin` is also bundled in `resourcey.util.models` for
polymorphic models keyed by a `kind` discriminator — likewise vendored from
the SDK with no dependency.

## Migrations

Database revisions are generated with [Alembic](https://alembic.sqlalchemy.org/)
from the current resource models. There is no `resourcey migrate` wrapper:
SQLAlchemy is the schema of record, so Alembic is driven directly against
`Base.metadata` (see the examples' `migrations/` directories):

```bash
alembic revision --autogenerate -m "add widget table"
alembic upgrade head                # apply
alembic downgrade -1                # one step back
```

**Generated revisions are drafts.** Alembic's autogeneration cannot detect
table or column *renames* — a rename looks like a drop followed by a create,
which loses data. Review every generated revision before applying it.

## Status

Early-stage. The roadmap is tracked in
[GitHub issues](https://github.com/tofarr/resourcey/issues). See `AGENTS.md`
for contributor rules and `specs/` for the formal Quint specifications.

## License

MIT
