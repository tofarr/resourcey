# resourcey

A resource-oriented Python web framework built on FastAPI, SQLAlchemy, and
Alembic — **progressive enhancement with escape hatches to the underlying
stack.**

## Installation

```bash
pip install resourcey
```

```bash
uv add resourcey
```

Optional extras pull in a driver for the parts you use:

| Extra | Adds | For |
|---|---|---|
| `resourcey[mongodb]` | `motor` | the Mongo backend |
| `resourcey[s3]` | `boto3` | the S3 file-store medium |
| `resourcey[redis]` | `redis` | `RedisTrigger` / `RedisChannel` |
| `resourcey[webhooks]` | `httpx` | `WebhookTrigger` |
| `resourcey[dev]` | pytest, ruff, mypy, test drivers | working on `resourcey` itself |

## Why?

Most web frameworks either hide the database entirely or make you wire every
layer by hand. `resourcey` takes a middle path: you declare a **resource**
with enough metadata to describe how it should be stored, validated, and
exposed, and the framework generates a service for you. When the abstraction
gets in the way, you drop back a rung — to FastAPI, SQLAlchemy, or Alembic —
and nothing breaks.

The guiding principles:

* **Resource-oriented.** A resource is the central unit. Define it once — for
  SQL, by declaring your SQLAlchemy model — and derive the DTO, the REST
  models, the service, and the permission checks from that single declaration.
* **Progressive enhancement.** Each layer is opt-in. Use the generated
  service, or write your own route that calls the repository directly, or
  query SQLAlchemy yourself. The layers compose; they don't trap you.
* **Escape hatches.** FastAPI, SQLAlchemy, and Alembic are first-class
  citizens, not implementation details. You can always reach them.

## What a resource gives you

Declaring a resource produces:

* **A REST service** exposing the standard actions:
  `create`, `read`, `update`, `delete`, `search`, `count`, `batch_read`, plus one
  app-level `POST /batch-edit` spanning every resource.
* **Pydantic models** for request/response validation, projected from the
  resource's DTO declaration — create / update / read / search shapes, never
  hand-written.
* **Persistence**, from whichever backend you declared it against. For SQL the
  workflow is **model-first**: your SQLAlchemy model is the schema of record and
  the DTO is inferred from it, so the mapping you already write stays
  authoritative (and Alembic autogenerates from that same metadata). The Mongo
  and list backends are DTO-first, since they have no schema of record.
* **Search filtering** — `search` and `count` accept `<field>__<op>` query
  params (`?title__contains=ali&id__gt=5`). A field is filterable exactly when
  the read model exposes it, so a hidden field is rejected `400`; a resource can
  declare a richer filter via `get_search_filter_type`. Filters lower to a
  storage-agnostic tree and are pushed into the SQL `WHERE` clause; an
  unconvertible filter fails loudly unless the resource opts into an in-memory
  fallback.
* **Sorting and cursor paging** — `?sort=<field>&desc=true` over any sortable
  (i.e. readable) field, with opaque keyset cursors instead of `offset`.
* **Caching** — an `ETag` / `Last-Modified` strategy chosen from the resource's
  shape, marked `private` whenever a response is caller-scoped.
* **Authorization** — pluggable `Policy` rules reduce a principal to a
  per-action filter: API-key / cookie authentication, per-app roles, and a
  stored users / groups / roles RBAC store.

## Storage backends

A resource is declared against one of three backends; the action contract,
paging, sort, filters, and cache headers are identical across all three.

| Backend | Base class | Storage |
|---|---|---|
| SQL | `resourcey.sql.sql_resource.SqlResource` | SQLAlchemy 2 (async) table |
| Mongo | `resourcey.mongo.mongo_resource.MongoResource` | an async `motor` collection |
| List | `resourcey.list.list_resource.ListResource` | an in-process list of Pydantic objects |

(There are no `__init__.py` re-exports, so import from the module that defines
the class — `from resourcey.sql.sql_resource import SqlResource`.)

A **list-backed** resource is read-only: it narrows its actions to
`read` / `search` / `count` / `batch_read` and serves data already modelled as
Pydantic objects (country codes, feature flags, catalog entries) without
copying it into a table. Because the objects already exist as Pydantic models,
there is no schema generation — the model is projected onto the DTO the REST
shapes derive from, and (being read-only) there is no create/update model and
no columns. The list *is* the storage, so there is no table and no migration.

```python
from pydantic import BaseModel

from resourcey.core.manifest import Manifest
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

That manifest serves `GET /countries` (search), `GET /countries/count`,
`GET /countries/batch-read`, and `GET /countries/{id}` — and nothing else: a
write route is never mounted, so a `POST` is a `405`.

`defensive=True` is the default: every object the resource outputs is a deep
copy of the stored object, so a caller cannot mutate the served collection
through a result. Pass `defensive=False` to serve the stored objects directly.

## The `core` package — DTO, Resource, Service, Manifest

`src/resourcey/core/` is a deliberately minimal package that states the
architecture in terms of a **DTO**, a **Resource**, a **Service**, and a
**Manifest**. It is the foundation every backend and transport layer is built
on.

The split separates **the DTO** (the data object you work with internally) from
**the six REST models** (the wire shapes), which the older code entangled, and
does away with six hand-written models per resource — those are now *derived*.

Here is a whole manifest against the SQL backend — the ORM models are the only
declarations:

```python
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.core.manifest import Manifest
from resourcey.sql.sql_resource import SqlResource


class Base(DeclarativeBase):
    pass


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"))
    body: Mapped[str]


manifest = Manifest(resources=(SqlResource(Thread), SqlResource(Message)))
```

From those two classes `create_app(manifest)` serves `POST /threads` and
`GET /threads` (search), `GET|PATCH|DELETE /threads/{id}`, `GET /threads/count`,
and `GET /threads/batch-read` — and the same set for `messages` — plus
`POST /batch-edit`, which spans both resources, and the OpenAPI docs.

`POST /batch-edit` takes one ordered list of `create` / `update` / `delete`
nodes, each naming its target resource, and answers with a positionally aligned
list. Nodes apply in the order given, so a later node sees an earlier node's
write. The first failure aborts the batch; rollback is best effort, holding
wherever the backend provides a shared transaction (SQL resources on one
session factory roll back together). It is mounted by default — pass
`create_app(manifest, batch_edit=False)` to turn it off, or
`batch_edit_path="/ops/edits"` to move it. The node count is capped by
`batch_edit_max_edits`, which defaults to `RequestLimitsConfig.max_batch_edits`
(`APP_MAX_BATCH_EDITS`, 100). A resource opts out by declaring no write action,
and a hidden resource is never reachable through it. It is the **only** batch
write surface: there is no per-resource `/batch-edit`, and each node is
authorized against its own singular action.

SQLAlchemy is the schema of record: the DTO and the six REST models are
inferred from the ORM model, so a developer keeps the mapping they already
write. The Mongo backend is DTO-first instead (there is no schema of record);
see its section below.

The five files:

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

* **`resource.py`** — `Resource` is a genuine ABC: every method is abstract, so
  `core` holds no backend behaviour and a backend supplies the whole contract.
  It is generic over the DTO and the identifier type (`Resource[T, K]`).
  `get_service(ctx)` is **async** and takes an optional call-scoped
  `MutableMapping`; awaiting it builds the service (a backend may resolve a
  connection first) and the returned `Service` is the async context manager that
  owns the storage, so the call site is
  `async with await resource.get_service(ctx)`. `get_supported_actions()` is the
  single action declaration (there is no `actions` property), and
  `get_exposed_resource()` composes on top of it — the exposed resource's
  declaration wins outright.
* **`service.py`** — `Service` is generic over the DTO, declares the eight
  actions, and *is* the async context manager; a call before `__aenter__`
  raises clearly. `Action` is the action enum, `Edit` the
  create/update/delete union, and `ForbiddenError` / `NotFoundError` /
  `ServiceError` live here.
* **`errors.py`** — the framework error base `ResourceyError` and the
  storage-neutral errors a backend raises and the transport maps:
  `ResourceyConfigError`, `InvalidInputError`, `UnsupportedFilterError`,
  `ConflictError`.
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
configurable `DependencyBuilder` (its default, `OpenDependencyBuilder`, opens
the resource's own service over the request-scoped `ctx` and applies no
authentication). `util` is the bottom layer: an isolation test pins the layer
ranks `util < core < {sql, mongo, list, view, filestore, http, config, cache,
encryption, auth, jobs, tasks, triggers, realtime}`, so no module imports a
higher layer at runtime.

### Configuration — env-driven edges

`core` stays config-free (a service is built from the objects it is handed),
while each env-driven *edge* is a `BaseConfig` block living with what it
configures: `SqlConfig` in `sql/`, `EncryptionKeysConfig` in `encryption/`,
`FileStoreConfig` in `filestore/`, `RealtimeConfig` in `realtime/`,
`ApiKeysConfig` / `IdpConfig` / `SessionCookieConfig` in `auth/`,
`BackgroundTasksConfig` in `tasks/`, `JobsConfig` in `jobs/`. All read the one
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
wrapper script to populate the environment. A pluggable medium or channel is
chosen by name with a `LazyField` (`APP_MEDIUM_CLASS`, `APP_CHANNEL_CLASS`), so
swapping the implementation needs no code change.

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
`Authenticator` (API-key, cookie, and OAuth / OIDC authenticators) that produces
a `Principal`, and a `PolicyResolver` that maps that principal to `Policy` rules.
The built-ins are `AllowAll` / `DenyAll` / `ReadOnly` / `Owner`, with a
per-app role vocabulary (`RolePolicyResolver`) and a store-backed RBAC resolver
(`RbacPolicyResolver`) over `users` / `groups` / `roles` / `role_permissions` /
`resource_acls` tables. An `AuthorizedService` enforces the reduced filter per
action: a denied create is `403`, an out-of-scope read/update/delete is `404`,
and a denied search/count yields an empty page / `0`.

### Who am I — the `me` endpoint

`register_me_routes(app, authenticator=..., user_resource=...)` (issue #150)
mounts `GET` / `POST /me` **after** `create_app`, reading the same `Authenticator`
the app's builder uses, so it works uniformly across the API-key, cookie, and
OAuth methods. The response is an [OIDC UserInfo](https://openid.net/specs/openid-connect-core-1_0.html#UserInfo)
object: `sub` (the caller's internal principal id — always present), plus the
framework's `kind` / `roles` / `scopes` and, for an external-IdP caller,
`external_id`. When a `user_resource` is supplied, the caller's **own** row adds
profile claims (`email`, `preferred_username`, …); an unavailable claim is
omitted, never `null`. The route reads that row directly (not through the
`users` policy) and can only ever return the id the credential already proved, so
an admin-only `users` surface does not block a caller from seeing its own `me`.
It is guarded by the **strict** principal dependency: an absent or invalid
credential is a `401` regardless of the builder's posture. See
[`examples/04_simple_roles`](https://github.com/tofarr/resourcey/tree/main/examples/04_simple_roles) / [`07_oauth`](https://github.com/tofarr/resourcey/tree/main/examples/07_oauth).

### OAuth / OIDC (external identity providers)

The OAuth method (issue #151) federates to an external IdP. `OAuthAuthenticator`
verifies an `Authorization: Bearer` token — issuer-keyed client lookup, JWKS
signature check, and `alg` pinned to the client row's allowlist (no algorithm
confusion) — maps `(iss, sub)` through an `ExternalIdentity` table to a local
user, and validates that user against the local store (a missing / disabled user
is rejected: the local store stays authoritative over the IdP). Provider
configuration and tokens are **resources**, so they live in the environment
(`APP_OAUTH_CLIENTS_<n>_*`) or the database; the refresh token lives only,
encrypted at rest, in an `oauth_tokens` table, and refresh is serialized across
processes with a lease / compare-and-swap column. `configure_oauth(...)` wires
the whole subsystem in one call and `register_oauth_routes` mounts the
interactive login / callback / refresh flow (BFF session, PKCE). See
[`examples/07_oauth`](https://github.com/tofarr/resourcey/tree/main/examples/07_oauth).

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
