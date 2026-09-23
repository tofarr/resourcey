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
* `config` — env parser usage and `DiscriminatedUnionMixin`.
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

### `v2/core` — the DTO / Resource / Service bottom layer

`src/resourcey/v2/core/` is a new, deliberately minimal package (issue #75)
that runs **parallel to** the existing packages: it states the architecture in
terms of **DTO**, **Resource**, **Service**, plus a **Manifest**, and the
existing modules are migrated onto it later. Nothing existing is removed by it,
and it is not a refactor.

Four files, no `__init__.py`:

* `dto.py` — `DTO` is a plain declaration class (not a Pydantic model). Fields
  carry ordinary Pydantic annotations plus a `DtoField` describing how each
  projects into the six REST shapes via six `in_*` flags; `DtoField` also
  carries a logical default with precedence *client value → logical default →
  `MISSING`*. `DTO.__init_subclass__` applies the `id`/timestamp conventions
  and wraps every field `ann | Missing = MISSING`. `Missing` is a usable
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
* `resource.py` — `Resource` is derived from the DTO and is storage-agnostic:
  it knows the DTO, the derived REST models, and the action surface, but not
  where the data lives. A backend subclasses it and implements `build_service`.
  `get_service(ctx)` is **sync**, takes an optional call-scoped
  `MutableMapping`, and returns a `Service` that is the async CM.
  `get_supported_actions()` is the single action declaration (no `actions`
  property); `get_exposed_resource()` composes on top of it and the exposed
  resource's declaration wins outright.
* `service.py` — `Service` is generic over the DTO, declares the eight actions,
  and *is* the async context manager; a call before `__aenter__` raises. It
  carries no storage: session-per-service and session-per-operation are both
  expressible and core privileges neither. The shared rule is *whoever opens
  the storage owns its commit and close; a resource that finds storage already
  in `ctx` reuses it and neither commits nor closes it*.
* `manifest.py` — `Manifest` owns the resource set and lifecycle, and asserts
  at construction that every `get_supported_actions()` names only real
  `Action` members (a typo would otherwise silently drop a route).

HTTP construction (`create_app`) is **not** part of `v2/core` — it belongs to
the transport layer.

### `v2/sql` — the SQLAlchemy backend (issue #78)

`src/resourcey/v2/sql/` is the SQL backend on top of `v2/core`, laid out like
`core` (flat files by role, no `__init__.py`):

* `resource.py` — `SqlResource` (a `Resource` subclass) is handed an async
  session maker **as a required constructor argument** plus an optional
  `base=` (default `V2Base`, also exported). It either **adopts** the ORM model
  recorded in the DTO's metadata (see below) or **generates** one from the DTO
  onto `base`. Either way the model materialises, so building the resources of
  a manifest pulls the whole current schema into the base's metadata — which is
  what migrations enumerate.
* `service.py` — `SqlService` holds the call-scoped `ctx` and the session
  factory and implements the eight actions. `search` does keyset cursor
  pagination ordered by the identifier; `sort` / `desc` / `filters` are
  declared but raise `NotImplementedError` until a later PR adds them together.
* `sqlalchemy_2_dto.py` — `sqlalchemy_2_dto(model)` converts an ORM model into
  a DTO declaration: column types map back to Python annotations, the primary
  key becomes `id_field_name`, nullability becomes `ann | None`, and
  client-side defaults become logical defaults / `in_create_request=False`.
  The produced DTO records the **source model** under `MODEL_METADATA_KEY`
  (`"v2.sql.model"`) in its `metadata`, the handoff `SqlResource` reads back
  through the constant, never a literal. **Plain columns only**: a FK column is
  a plain scalar field; `relationship()`s are not projected (a known
  limitation — nested projection is its own future issue).
* `migration.py` — `generate_migration(base, *, database_url, message,
  migrations_dir="migrations")` enumerates every model on a declarative base
  and drives Alembic autogeneration over its metadata, returning the revision
  path. `base` may be a class or a `package.subpackage:Base` string; an
  async-driver URL is normalised to its sync counterpart; the migrations
  directory is created on demand. The `__main__` block **generates only**
  (`python -m resourcey.v2.sql.migration myapp.models:Base -m "initial"`);
  applying is the test suite's job, not a `-m` invocation's. Every revision is
  a review-required draft (Alembic cannot detect renames).
* `cursor.py` — tamper-proof keyset cursor encode/decode plus the keyset
  `WHERE` predicate; the cursor is a JWE from `v2/encryption`.

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

### `v2/` isolation

`v2/core`, `v2/sql`, and `v2/encryption` are **parallel** to the existing
packages — nothing existing is removed by them and they are not a refactor.
The old `v1` SQL packages/modules (and the old `resourcey.encryption`) stay in
place until a follow-up removal. A test asserts that no module under `v2/`
makes a **runtime** import of any `resourcey` code *outside* `v2/` (same static
AST walk, now covering `core`, `sql`, and `encryption` in one rule),
`if TYPE_CHECKING:` imports still allowed. `v2/sql` implements whatever small
helpers it needs locally rather than reaching for `resourcey.util`.

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
