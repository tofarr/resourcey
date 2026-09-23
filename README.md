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
* **Migrations** — Alembic autogeneration from the current models, so you
  can derive schema changes from your resource declarations.
* **Permissions** — the resource declares which actions a role may perform,
  and the framework computes the effective permission set per user.

## Storage backends

A resource is declared against one of three backends; the action contract,
paging, sort, filters, and cache headers are identical across all three.

| Backend | Base class | Storage |
|---|---|---|
| SQL | `SqlResource` | SQLAlchemy 2 (async) table |
| Mongo | `MongoResource` | an async `motor` collection |
| List | `ListResource` | an in-process list of Pydantic objects |

A **list-backed** resource is read-only: it narrows its actions to
`read` / `search` / `count` / `batch_read` and serves data already modelled as
Pydantic objects (country codes, feature flags, catalog entries) without
copying it into a table. Because the objects already exist as Pydantic models,
there is no schema generation — the model *is* the read model, and (being
read-only) there is no create/update model and no columns. The list *is* the
storage, so there is no table and no migration.

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
resource = ListResource(models=countries, path="countries")

manifest = ResourceManifest(resources=(resource,))
```

`defensive=True` is the default: every object the resource outputs is a deep
copy of the stored object, so a caller cannot mutate the served collection
through a result. Pass `defensive=False` to serve the stored objects directly.

## The `v2/core` package — DTO, Resource, Service, Manifest

`src/resourcey/v2/core/` is a new, deliberately minimal package that states the
architecture in terms of three constructs plus a manifest. It runs **parallel
to** the existing packages; the existing modules are migrated onto it later, as
an iterative follow-up, and nothing existing is removed by it.

The split separates **the DTO** (the data object you work with internally) from
**the six REST models** (the wire shapes), which the older code entangled, and
does away with six hand-written models per resource — those are now *derived*.

```python
from resourcey.v2.core.dto import DTO
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import SqlResource


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
  `DtoField` also carries a logical default (`logical_default_value` /
  `logical_default_value_factory`), whose precedence is *client value →
  logical default → `MISSING`*. `DTO.__init_subclass__` applies the `id` /
  timestamp conventions and wraps every field as `ann | Missing` defaulting to
  `MISSING`, so an omitted field is distinguishable from an explicit `None`.
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
  first backend (a DTO-derived table over an injected async session factory;
  the backend is chosen explicitly at construction, not inferred from config).
  `get_service(ctx)` is **sync** and takes an optional call-scoped
  `MutableMapping`; the returned `Service` is the async context manager that
  owns the storage. `get_supported_actions()` is the single action declaration
  (there is no `actions` property), and `get_exposed_resource()` composes on
  top of it — the exposed resource's declaration wins outright.
* **`service.py`** — `Service` is generic over the DTO, declares the eight
  actions, and *is* the async context manager; a call before `__aenter__`
  raises clearly. `Action` is the action enum.
* **`manifest.py`** — the `Manifest` owns the resource set and its lifecycle,
  and asserts at construction that every resource's supported actions name only
  real `Action` members (a typo would otherwise silently drop a route).

Storage ownership follows one rule, letting session-per-service and
session-per-operation both live:

> Whoever opens the storage owns its commit and close. A resource that finds
> storage already in `ctx` reuses it and neither commits nor closes it.

`ctx` is a plain `MutableMapping` keyed by module-level sentinels, so a caller
can pre-seed storage (the escape hatch) and every resource in the call adopts
it. `AppContext` (app-scoped) stays a separate concept. HTTP construction
(`create_app`) is deliberately **not** part of `v2/core` — it belongs to the
transport layer. A test asserts no module under `v2/core` makes a runtime
import of any other `resourcey` package, which pins it as the bottom layer.

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

## Permissions (users, groups, roles)

`resourcey` models users, groups, and roles so that a per-user permission set
can be computed for every resource action. The permission engine is reusable
across applications and was abstracted out of
[`ohev2`](https://github.com/tofarr/ohev2), where it solved the same problems
in a domain-specific setting.

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
from the current resource models. The `resourcey migrate` CLI wraps Alembic so
you don't need to run `alembic init` or hand-write an `alembic.ini`:

```bash
# Materialise env.py + versions/ in the migrations directory (idempotent)
resourcey migrate init

# Autogenerate a draft revision from your resource models
resourcey migrate autogenerate -m "add widget table"

# Apply / roll back
resourcey migrate upgrade          # to head
resourcey migrate downgrade -1     # one step back
```

The migrations directory is configured under the `migrations` key of
`FrameworkConfig` (env prefix `RESOURCEY_MIGRATIONS_`). The resource set is the
**app-level** `FrameworkConfig.manifest` field (env `RESOURCEY_MANIFEST`) —
a `module:attr` path to the app's `ResourceManifest`, which owns the resource
instances and materialises their tables. The same manifest drives the REST
service layer and RBAC, so migrations never diverge from what the app serves:

| Env var | Default | Purpose |
| --- | --- | --- |
| `RESOURCEY_MANIFEST` | `""` | Dotted/colon path (`module:attr`) to the app's `ResourceManifest` instance. `env.py` imports it and calls `materialize()` before autogenerating. |
| `RESOURCEY_MIGRATIONS_MIGRATIONS_DIR` | `migrations` | Directory holding `env.py` and `versions/`. |

```bash
RESOURCEY_MANIFEST='myapp.app:manifest' resourcey migrate autogenerate -m "init"
```

**Generated revisions are drafts.** Alembic's autogeneration cannot detect
table or column *renames* — a rename looks like a drop followed by a create,
which loses data. Review every generated revision before applying it. Run
`alembic` directly to escape the wrapper when you need full control.

## Status

Early-stage. The roadmap is tracked in
[GitHub issues](https://github.com/tofarr/resourcey/issues). See `AGENTS.md`
for contributor rules and `specs/` for the formal Quint specifications.

## License

MIT
