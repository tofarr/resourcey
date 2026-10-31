# AGENTS.md — rules for agents and contributors working in `resourcey`

Persistent memory for this repository. Everything here applies to **every**
conversation. Subsystem detail lives in on-demand skills
(`.agents/skills/`); load the matching one before working in that area.

## Stack & tooling

* Python ≥ 3.11, asyncio-first. Never use blocking I/O on the request path.
* Manage dependencies with `uv`. Never hand-edit `uv.lock`; use
  `uv add/remove/sync`.
* FastAPI for HTTP. Pydantic v2 for all request/response schemas.
* SQLAlchemy 2 async ORM + asyncpg. Alembic for migrations.
* Quint for formal specs; every behavioral change to a resource must be
  reflected in `specs/` and verified with `quint typecheck` / `quint test`.

## Release state

Releases are published to PyPI as `resourcey` from a tagged GitHub release.
**Load the `release` skill before bumping a version, tagging, or touching
`release.yml`** — it carries the procedure, the recovery steps for a mis-tagged
release, and the troubleshooting table.

The one rule worth repeating here: the version lives in **two** places —
`pyproject.toml` and `uv.lock` — so bump it with `uv version <x.y.z>`, which
updates both. Editing `pyproject.toml` by hand leaves the lock stale: CI's
`uv sync` silently rewrites it, but `uv lock --check` (and the release build)
fails.

Still pre-1.0, so backward-compatibility shims are not required when changing
the public API: removed query params (e.g. a dropped `offset` after switching
to cursor pagination) do not need to be rejected or aliased — they may simply
be ignored. Optimize for a clean, minimal API surface over migration
ergonomics.

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
  `auth/`, `filestore/`). No `models/`/`routes/`/`services/` subfolders.
* Files inside a feature directory are flat and prefixed with the feature name
  for global uniqueness: `auth_policy.py`, `file_routes.py`, `jobs_model.py`.
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

## No `__all__` exports lists

Do not add `__all__` to modules. The codebase uses no wildcard imports, so an
explicit exports list is pure repetition of the names already defined at module
scope. Keep the public API implicit: every non-underscore-prefixed name is
importable, and consumers import the names they need directly.

## Vendored utilities

`resourcey.util.env_parser` and `resourcey.util.models` (including
`DiscriminatedUnionMixin`) are vendored from the OpenHands Software Agent SDK.
They must remain self-contained: **no `openhands` import may be introduced**.
When upgrading behaviour from upstream, copy the logic, do not add a
dependency.

## Core design principles

* **Resource-oriented.** A resource is the central unit. One declaration
  drives Pydantic schemas, SQLAlchemy models, the REST service, migrations,
  and permission checks.
* **Progressive enhancement.** Each layer is opt-in and composable. The
  generated service can be replaced by a hand-written route that calls the
  repository, or by raw SQLAlchemy — nothing breaks when you drop a rung.
* **Escape hatches.** FastAPI, SQLAlchemy, and Alembic are first-class. Never
  hide them behind an abstraction that cannot be bypassed.
* **Runtime-selected routes use `DependencyBuilder.build_service`.** A route
  whose target resource comes from the request body (the app-level
  `POST /batch-edit`) cannot use `get_service_dependency`, because FastAPI
  introspects a static signature. It calls `build_service` and enters the
  returned `Service` itself. Any builder that wraps the service in
  `get_service_dependency` **must** override `build_service` too, or that route
  bypasses the wrapping — which for an authorizing builder is an authorization
  bypass. Pinned by `tests/unit/test_batch_routes.py`.

## Framework map

Layers, ranked bottom-up. No module may import a strictly-higher project layer
at runtime (pinned by `tests/unit/test_isolation.py`; `if TYPE_CHECKING:`
imports are allowed):

```
util < core < {sql, mongo, list, view, filestore, http, config, cache,
               encryption, auth, jobs, tasks, triggers, realtime}
```

`util` imports no project package at all. `core` holds exactly
`{dto, errors, manifest, resource, service}.py` and imports only `util`.

| Layer | What it owns | Skill |
|---|---|---|
| `util` | `DiscriminatedUnionMixin`, env parser, `Missing` sentinel, cursor codec, naming, `Singleton`, `SearchFilter`, `SortOrder`, secret serialization | `config`, `services` |
| `core` | `DTO` / `DtoField` / `RestModels`, `Resource` ABC, `Service` + `Action` + `Edit` union, `Manifest`, errors | `services` |
| `config` | `BaseConfig` (per-class cache, one process-wide prefix), `LazyField` | `config` |
| `encryption` | `EncryptionService` (values, JWE cursors, auth tokens) + key config | `services` |
| `cache` | `CacheHeader`, strategies, `CacheStore`, `CachedResource`, the default-policy mixin | `services` |
| `sql` / `mongo` / `list` | the three storage backends | `backends` |
| `view` | `ResourceView` — the configured narrowing wrapper | `backends` |
| `filestore` | `FileStore` mediums, `FileResource`, `register_file_routes` | `filestore` |
| `http` | `create_app` / `add_to_app`, `DependencyBuilder`, `register_routes`, `register_batch_routes`, error envelope, `RequestLimitsConfig` (the `search` / `batch_read` caps) | `transport` |
| `auth` | principals, authenticators, policies, roles, RBAC store, OAuth, `me` | `auth-rbac` |
| `tasks` | cron parsing, `BackgroundTaskScheduler` | `background-work` |
| `triggers` | `Trigger`, `TriggeredResource`, `WebhookTrigger`, `RedisTrigger` | `triggers-realtime` |
| `realtime` | `Channel`, `add_realtime`, `add_asyncapi` | `triggers-realtime` |
| `jobs` | durable claimable rows, `JobRunner` | `background-work` |

Two structural rules the isolation test also pins: the `core` file set is
exactly the five files named above, and no framework module imports
`openhands`.

## Repo layout

* `src/resourcey/` — the framework (the table above).
* `examples/01_message_board` … `examples/10_jobs` — standalone `uv` projects,
  each with its own `pyproject.toml`, `.venv`, committed `.env`, `tests/`, and
  a README. They are excluded from the root ruff/mypy config and linted as
  standalone projects; CI runs each one's `uv run pytest` as an E2E job.
  `01_message_board` is the reference app (model-first `SqlResource` over ORM
  models, shared `SqlSessionManager`, `create_app`, `APP_*` config, Alembic
  against `Base.metadata`); `02_mongodb` is its Mongo counterpart (DTO-first
  `MongoResource`, embedded `mongomock`, `migrate_document` instead of
  migrations). Examples 03–07 are the four auth rungs plus the file store;
  08–10 are the trigger, realtime, and durable-job rungs. Read the relevant
  example README before changing one.
* `.vscode/launch.json` + `tasks.json` — debug configs, one per example
  (`uvicorn <module>:app --env-file .env`, ports 8081–8090, `cwd` set to the
  example). `tasks.json` runs `uv sync` and, for SQL examples, the Alembic
  migration first.
* `specs/*.qnt` — Quint specs. `Makefile` `specs` target and
  `.github/workflows/ci.yml` run them.

## On-demand skills

Load the skill for the area you are touching:

* `services` — the `core` contract (DTO, Resource, Service, Manifest),
  conditional writes, caching, encryption. Read this before changing any
  backend-agnostic behaviour.
* `backends` — `SqlResource` / `MongoResource` / `ListResource` / `ResourceView`
  and their converters, paging, and sort/filter surfaces.
* `transport` — `create_app`, `DependencyBuilder`, `register_routes`, the
  action→route mapping, error envelope, and cache headers.
* `auth-rbac` — principals, authenticators, policies, simple roles, stored
  RBAC, OAuth/OIDC, the `me` endpoint.
* `filestore` — the medium seam and the `files` surface.
* `triggers-realtime` — edit-event triggers and the WebSocket channel.
* `background-work` — cron tasks, the scheduler, and durable jobs.
* `config` — env parsing, `BaseConfig`, `LazyField`, `DiscriminatedUnionMixin`.
* `migrations` — Alembic autogeneration.
* `testing` — hermetic test setup and how to run the suite.
* `quint-specs` — when to update specs and how to verify them.
* `release` — version bump, tag, publish, and recovery from a failed release.
* `rest-api-routes` — wire conventions (paths, verbs, batch bodies, errors).
* `pr-quality-checks` — the gates to run before opening or updating a PR.
* `pr-review-checklist` — what to verify when reviewing a PR.

## Issue tracking & roadmap

Work is tracked via [GitHub issues](https://github.com/tofarr/resourcey/issues).
Create an issue before starting non-trivial work; reference it in commits and
PRs.
