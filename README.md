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
