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
* `examples/01_message_board`, `02_mongodb`, `03_api_key_auth`,
  `04_simple_roles`, `05_full_rbac` — standalone
  `uv` projects, each with its own `pyproject.toml`, `.venv`, and committed
  `.env`. They are excluded from the root ruff/mypy config and linted as
  standalone projects. `01_message_board` is the **`v2` reference app** (issue
  #113): model-first `SqlResource` over ORM models, a shared `SqlSessionManager`
  in the manifest's `managers=`, `create_app`, `APP_*` config, and Alembic
  driven directly against `Base.metadata` (there is
  no `resourcey migrate` in `v2` — that CLI reads the `v1` `ResourceyBase` /
  `FrameworkConfig.manifest`). `02_mongodb` is its **`v2` Mongo counterpart**
  (issue #80): DTO-first `MongoResource` over an embedded (`mongomock`) client,
  a shared `MongoClientManager` in the manifest's `managers=`, `create_app`, and
  no migration step (the schema is implicit and
  `migrate_document` is the opt-in hook). `03_api_key_auth` is the **`v2`
  authentication app** (issue #124): the same model-first message board as 01,
  secured by an `AuthorizedDependencyBuilder` (authenticating with an
  `ApiKeyAuthenticator`, granting `AllowAll`) passed to `create_app`, over a
  config-list key resource (`ApiKeysConfig` → `config_api_key_resource` /
  `config_api_key_view`) built from `APP_API_KEYS_*` — no auth table, no
  `DEPENDENCY_BUILDER_CLASS`
  (the builder is constructed explicitly in `app.py`). The no-auth transport
  default is named `OpenDependencyBuilder`, so `build_app` always passing the
  API-key builder reads as the deliberate choice it is rather than a hidden
  fallback. `04_simple_roles` is the **`v2` authorization app** (issue #132): the
  same message board, but with a per-app `Role` vocabulary carried on each API
  key (`APP_API_KEYS_<n>_ROLES_<m>`) and a single `RolePolicyResolver` mapping
  role -> policy (global + per-resource). Reads are **public by default** — the
  builder runs `Posture.OPTIONAL` (an absent credential is anonymous, a
  *presented but invalid* one is still `401`) and every resource falls to a
  resource-level `ReadOnly` default, so anonymous/un-roled callers read
  `threads` / `messages`; roles gate the **writes**: `ADMIN` full access,
  `MODERATOR` creates / updates / deletes `messages`, `USER` creates messages
  and edits only its own (the union of `ReadOnly` + `Owner(author_id)`, so
  read-all / write-own — the "read all of X, own rows of Y" rule). It also
  **stores its principals**: a `User` ORM model (`users` table) served read-only
  by a `SqlResource` wrapped in a `ResourceView` (`simple_roles/user.py`), with
  the `ApiKeyAuthenticator`'s optional `user_resource=` validating each key's
  `PRINCIPAL_ID` against a live, `enabled` row (a missing / disabled principal
  ⇒ `401`), so a stored principal's flag is authoritative over the credential.
  The `users` surface is **admin-only and not public** (deliberately absent from
  the resource-level defaults, so no role and no anonymous caller reaches it;
  only `ADMIN`'s global `AllowAll`). Two fixed principals are seeded by
  the committed Alembic migration and by `simple_roles/seed.py` (the ids the
  `.env` keys name). This is 04's identity-store rung; the store-backed groups /
  roles / per-request resolution land in `05_full_rbac`. `v2` does
  no `.env` loading, so its run/debug commands pass
  `uvicorn --env-file .env` / `uv run --env-file .env`. `05_full_rbac` is the
  **`v2` store-backed RBAC app** (issue #133, Part 3 of the auth roadmap): the
  same board, but the credential carries only a `principal_id` and the roles /
  groups / permissions live in real `users` / `groups` / `group_users` /
  `roles` / `group_roles` / `role_permissions` / `resource_acls` tables, resolved
  **per request** by `RbacPolicyResolver` (group → role → permission, scoped to
  the target resource) — `admin` full access, `viewer` read-only, `author`
  read-only on `threads` but `Owner`-scoped (`author_id`) on `messages`, and a
  `DenyAll` never overriding another role's grant (union model). Multiple roles
  union their policies: `viewer` ∪ `author` reads all of `messages` but edits
  only its own. A `seed` module populates the store; the whole RBAC set is also
  served over the ordinary REST surface, with an `admin` grant on each table.
* `.vscode/launch.json` + `tasks.json` — debug configs for the examples. Each
  launches `uvicorn <app>:app` with `cwd` set to the example directory (so its
  `.env` applies) and `python` pointing at that example's `.venv`. Ports:
  8081 (01), 8082 (02), 8083 (03), 8084 (04), 8085 (05).

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

### `auth` and the `v2/auth` successor

`src/resourcey/auth2/` (issue #63), the **v1** API-key authentication seam, has
been **deleted** now that its `v2` port is complete. Authentication work lives
in **`src/resourcey/v2/auth/`**: `auth_principal.py` (the `Principal` /
`PrincipalKind` / `AuthResult` vocabulary, the `Authenticator` seam, the
`CompositeAuthenticator`, and the lenient/strict principal dependencies),
`auth_api_key.py` (the `ApiKeyAuthenticator`), `auth_cookie.py` (the
`CookieAuthenticator`), `auth_api_key_resource.py` (the DB-backed ORM `ApiKey` +
inner `SqlResource`, the config-list inner `ListResource`, the exposed
`ResourceView`s, and the key-generation helpers), `auth_api_key_service.py`
(`StoredApiKeyService` / `ConfigApiKeyService`, both exposing `find_by_key`),
and `auth_config.py` (`ApiKeysConfig` / `ApiKeyConfig` / `SessionCookieConfig`).
It imports **only `v2`**, and `v2/http` must not import `v2/auth` (the app
supplies the builder), so no cycle exists.

The remaining `v1` package `src/resourcey/auth/` (users, sessions, OAuth,
permissions) stays in place until it too is ported; `v2/auth` must never import
it at runtime (pinned by the `v2` isolation test).

Key semantics: the key is a `SecretStr` and is stored **only as a SHA-256
digest**; a presented key is validated by hashing it and searching for the
digest (`find_by_key`), never through the public query surface. The raw key is
disclosed exactly once, in the `201` create response, through the
`expose_secrets` serialization context — see `specs/api_key.qnt` /
`specs/auth.qnt`. Selection is the explicit `dependency_builder=` argument on
`create_app` / `add_to_app` (config-driven builder selection is a later rung).

### `v2/auth` authorization (issue #127)

Authorization (what a caller *may do*, as distinct from #118's authentication)
lives beside the API-key code in **`src/resourcey/v2/auth/`**:

* `auth_policy.py` — **`Policy`** (a `DiscriminatedUnionMixin`) reduces itself to
  a `SearchFilter` via `async def to_search_filter(user_id, action)`; built-ins
  **`AllowAll`** (`AllFilter`), **`DenyAll`** (`NoMatchFilter`), **`ReadOnly`**
  (`AllFilter` for read / search / count / batch_read, else `NoMatchFilter`). It is named
  `Policy`, not v1's `Permission`: a *permission* is the computed right, the
  *policy* is the rule that computes it (the project is pre-release, so clarity
  beats v1 compatibility; `specs/permissions.qnt` already calls the rule a
  `Policy`). The principal is an explicit argument to the reduction, not state
  on the policy, so a `Policy` stays storable; `user_id` is `None` for now and
  is the seam for the later users/groups/roles work. The built-in reductions use
  the unparameterized `AllFilter()` / `NoMatchFilter()` leaves (the pydantic
  parameterization `AllFilter[Any](...)` is a distinct class the SQL/Mongo
  translation registries do not know).
* `auth_authorized_service.py` — **`AuthorizedService`** wraps a resource's own
  service and enforces the reduced filter per action, porting v1's
  `SecuredService` semantics: a denied **create** raises `ForbiddenError` (403);
  an out-of-scope **read** / **update** / **delete** raises `NotFoundError`
  (404) so existence is not leaked; a denied **search** / **count** `and_`-and-pushes
  the permission filter down and yields an empty page / `0` (never an error);
  **batch_read** / **batch_edit** positions are `None`. It enters its inner
  (honouring "whoever opens the storage owns its commit and close", and not
  double-closing an inner it was handed already entered, as the dependency
  opens it) and delegates `serialization_context()` so a one-time secret reveal
  survives wrapping.
* `auth_authorized_dependency.py` — **`AuthorizedDependencyBuilder`** (a
  `DependencyBuilder`) composes two independent, pluggable seams: an
  **`authenticator`** (default a fail-closed `ApiKeyAuthenticator`) and a
  **`policy_resolver`** (default `AllowAllResolver`). After authentication it
  opens the resource service over the request ctx and wraps it in an entered
  `AuthorizedService` carrying the resolved policies. `posture` (default
  `Posture.REQUIRED`) selects whether an absent credential is a `401` or
  anonymous; `with_posture(...)` is the one-line override. `get_principal_dependency()`
  returns the strict/lenient principal dependency the transport adds to every
  route as a `dependencies=[...]` entry, so the authenticator's OpenAPI security
  scheme is declared without changing the route signature. The resolved policy
  set replaces #127's one-policy-per-app assumption.

### `v2/auth` authentication — the principal pipeline (issue #131)

`auth_principal.py` states the authentication half in one place:

* **`Principal`** (frozen, pydantic) — the authenticated caller: `id`
  (`None` = anonymous), `kind` (`USER` / `SERVICE` / `ANONYMOUS`), `roles`,
  `scopes`, `claims`. The three collections are three distinct layers and must
  not be conflated: `claims` is what the *credential says* (provenance only —
  never a decision input), `scopes` is a ceiling on *this credential*
  (intersected with the policy), and `roles` is what the *principal is part of*
  (sourced into a policy). `roles` is the **simple-roles** representation and is
  populated only when roles are few and credential-carried; the store-backed
  RBAC rung leaves it empty and resolves roles per request from `id`.
* **`AuthResult`** — one attempt's outcome, keeping **absent** (no credential)
  and **invalid** (presented but rejected) distinguishable, with
  `refresh_recommended` for a cookie past its freshness threshold.
* **`Authenticator`** (a `DiscriminatedUnionMixin`) — `authenticate(request) ->
  AuthResult`, with a `dependency()` returning the request-cached FastAPI
  dependency and a `challenge()` returning the `WWW-Authenticate` value.
  `optional_principal(authenticator)` / `required_principal(authenticator)`
  derive the **lenient** (anonymous on absent, `401` on invalid) and **strict**
  (`401` unless authenticated) dependencies from the one `AuthResult`, both
  sending the authenticator's challenge for absent and invalid alike.
* **`CompositeAuthenticator`** — an ordered chain (first authenticated wins; a
  seen-but-unauthenticated credential ⇒ invalid; else absent). Its dependency
  synthesises one `Depends(child.dependency())` parameter per child so every
  child's security scheme stays visible.

`ApiKeyAuthenticator` (`auth_api_key.py`) is the API-key `Authenticator`: it
looks the presented key's digest up on the inner (DB or config-list) key
resource and returns a `Principal` — a DB row's owner (`user_id`) wins, else a
`SERVICE` principal (optionally named by the config entry's `principal_id`). Its
optional `user_resource=` makes that principal id a **stored** one the key is
validated against: a key resolving to no live, `enabled` row is `invalid`, so a
stored principal's flag is authoritative over the credential (`None` keeps the
credential-only posture of examples 01-03). The store check lives in
`_principal_is_active` and is applied on **both** the builder path
(`authenticate`) and the standalone `api_key_dependency`, so a custom router
secured by the reusable dependency cannot accept a key whose principal is
missing or disabled; a store row lacking an `enabled` attribute is treated as
enabled (an identity-only store), while a missing row is a rejection.
`lookup_api_key` returns `None` for a
key that is inactive or past `expires_at`.
`CookieAuthenticator` (`auth_cookie.py`) mints/validates a JWE cookie
(`EncryptionService.create_jwe_token` / `decrypt_jwe_token`) whose `sub` is the
principal id; the cookie's `exp` is the **internal validation threshold** (when
to go back and re-check the principal), distinct from the browser cookie's
`Max-Age`, and `refresh_after` is an optional margin before it.
`SessionCookieConfig` (`auth_config.py`) holds the env-driven cookie settings.

`ForbiddenError` and the singular `normalize_action` (`COUNT`→`SEARCH`,
`BATCH_READ`→`READ`, `BATCH_EDIT`→`UPDATE`) live in `v2/core/service.py`;
`ForbiddenError` is mapped to `403 forbidden` in `v2/http/routes.py`'s error
envelope. Caching (#92) and authorization compose through a **caller-scope
seam**: `Policy.scopes_to_caller` (a `ClassVar`, default `True` = safe;
`AllowAll` / `DenyAll` / `ReadOnly` declare `False`) plus
`Service.response_is_private()` (`False` in `v2/core`, overridden by
`AuthorizedService` from the resolved policies). `v2/http/routes.py` forces
`private` onto the cache header when the service reports a caller-scoped
response, so a shared cache neither stores nor revalidates it — an `ETag` alone
cannot stop a shared cache serving one principal's slice to another. See
`specs/permissions.qnt` / `specs/cache_defaults.qnt`.

### `v2/auth` simple roles (issue #132)

Part 2 of the auth roadmap gives a deployment a small, **per-app** role
vocabulary carried on the credential and translated to policies in **one place**
(`auth_role.py`). Roles are per-app, never global: the same string means
whatever an app declares, so there is deliberately no process-wide role
registry. `AppRole` is the ergonomic `StrEnum` vocabulary (`Role.ADMIN`, no
magic strings) while the credential carries the **plain string** — `role_key` /
`role_keys` / `roles_from_credential` are the string conversion seam, so the
framework never imports app code to interpret a credential.

* `auth_policy.py` also gains **`PolicyResolver`** (a `DiscriminatedUnionMixin`,
  `async resolve(resource, principal) -> list[Policy]`) — the `Principal ->
  Policy` seam #131 introduced, with built-ins `AllowAllResolver` (the default,
  preserving #118 / #127's "a valid credential grants full access") and
  `DenyAllResolver`; and the **`Owner`** policy (own-rows-only): `AttrFilter` on
  `owner_field` (`user_id` by default) for the read-like / by-id write actions,
  `AllFilter` for **create** (a new row has no owner yet — the resource stamps
  it), and `NoMatchFilter` for an anonymous caller. `AuthorizedService`
  OR-combines the resolved policies (the union model), so a `DenyAll` from one
  role never overrides another's grant; an empty list is deny / fail-closed.
* `auth_role.py` — **`RolePolicyResolver`** is the single location an app
  expresses its `role -> policy` rules: a **global** `role_policies` map (a
  role's policies on every resource) plus a per-resource
  `resource_role_policies` map keyed by the resource's path, an explicit
  fail-closed `default` (empty unless the app opts in) with per-resource
  `resource_defaults` overrides. An unknown role therefore grants nothing. The
  static mapping is this rung; the store-backed resolver is part of Part 3
  (issue #133).
* Roles are carried on both credential types with **no extra store lookup**: a
  DB-backed / config-list `ApiKey` row's `roles` column / `ConfigApiKey.roles`,
  populated onto `Principal.roles` by `ApiKeyAuthenticator` (a row's own
  `principal_id` now wins over the authenticator's fixed one, and a
  present-but-unparseable id degrades to anonymous rather than adopting the
  shared fixed principal), and a cookie's `roles` JWE claim by
  `CookieAuthenticator`. The key `roles` field stays a normal, writable field on
  the DB-backed surface (a credential-carried role is an entry on the key's
  definition, not a secret) — which makes that resource a **privilege-assignment
  surface**, so access to it must be restricted to administrators; the
  config-list view hides `roles`
  from the read / search surface alongside `principal_id`.
* `auth_authorized_dependency.py` publishes the authenticated principal on the
  call-scoped `ctx` under `auth_principal.PRINCIPAL_CTX_KEY`, so a resource
  service (which receives the same ctx) can read it — e.g. an `Owner`-scoped
  resource stamping the owner on a create row. The key is a plain string so
  `v2/core` need not import `v2/auth`.

`specs/roles.qnt` pins the role → policy reduction and the union laws (in `make
specs` and CI): the `Owner` scoping (own rows for read / by-id writes, unscoped
create, denied anonymous), unknown-role / un-roled fail-closed defaults,
per-resource scoping (a role reading all of X but only its own rows of Y), the
union of several roles, and that a deny-only role never overrides a grant.

### `v2/auth` full RBAC — the stored rung (issue #133)

Part 3 replaces Part 2's app-level, credential-carried roles with a **store**.
Three modules, all importing only `v2`:

* `auth_rbac.py` — the ORM models + the exposed resource set, **model-first**
  like every `v2` SQL resource. `User` (`id`/`email`/`username`/`enabled`),
  `Group`, `GroupUser` (membership), `Role` (the stored counterpart of Part 2's
  role strings), `GroupRole` (role assignment to a group), `RolePermission` (the
  core RBAC unit: a `resource` name + a JSON-serialized `Policy`), and
  `ResourceAcl` (the materialized per-object grant, keyed by
  `(principal_id, resource_name)`). They live on a local `RbacBase` metadata.
  `policy_to_json` / `utc_now` are the small helpers. `RBAC_MODELS` and
  `rbac_resources(...)` expose the whole set as ordinary `SqlResource`s (so it
  can be seeded / administered over REST); an app that wants to hide a table
  registers a narrowing `ResourceView` instead.
* `auth_rbac_store.py` — **`RbacStore`** (the read seam: `policies_for`,
  `groups_for`, and the materialized-ACL `acl_ids` / `acl_id_subquery`) and its
  SQL implementation **`SqlRbacStore`**. Resolution **collapses at the store**:
  the query joins membership → role → permission filtered to the target
  `resource`, with `SELECT DISTINCT` so roles sharing a policy collapse. The
  session source is an `async_sessionmaker` **or** a zero-arg callable returning
  one (sync or async), so a store can be built before the app lifecycle (a
  `SqlSessionManager` only hands out makers once entered). `ACL_MAX_IDS = 100`
  caps the enumeration path (v1's bound carried forward); beyond it the join /
  subquery flavour is the answer. `policy_from_rows` skips a corrupt row rather
  than crashing or over-denying.
* `auth_rbac_resolver.py` — **`RbacPolicyResolver`**, Part 1's `PolicyResolver`
  seam against the store: for each request it takes the authenticated
  `Principal`, asks the store for the principal's groups and the resource-scoped
  policies, binds membership onto any `GroupMember` policy, and returns them;
  `AuthorizedService` OR-combines (union model — no deny-wins override; an
  empty set is fail-closed; an anonymous principal resolves to `[]`).
  `materialized_acl(...)` is the portable bridge that enumerates the (capped)
  ids into an `Acl` policy. **Freshness is explicit**: `cache_ttl` (default
  `None`) resolves every request, so an API key sees a membership change
  immediately; a positive value bounds staleness to (at most) the credential's
  own validation / refresh threshold. `invalidate(...)` is the write-through
  hook.

The vocabulary is Part 2's — `Owner`, `GroupMember`, `Acl`, plus the built-ins —
so Parts 2 and 3 share one `Policy` set. `GroupMember` is the principal-level
gate: membership is pre-bound by the resolver (`bind_member_groups`) and its
`to_search_filter` selects `on_match` / `on_mismatch` (`on_create` for create);
an unbound / empty target is fail-closed. `Acl` reduces to
`AttrFilter(id_field, InFilter(values=...))` — the set leaf from #136, one `IN`
predicate rather than a `K`-term `OR`.

`specs/rbac.qnt` pins the store resolution + union laws (in `make specs` and
CI): the policy reductions (`AllowAll` / `DenyAll` / `ReadOnly` / creator /
group-member / ACL), the group → role → permission walk, per-resource scoping,
`SELECT DISTINCT` collapse, fail-closed on unknown user / empty permission set,
multiple roles unioning, deny never overriding a grant, same-attribute ACLs
collapsing to one set, the set leaf agreeing with the join, the threshold
bounding a membership change, and the caller-scoping derivation.

### Storage backends and the shared paging base

The `v1` package `resourcey.resource` ships only the SQL backend,
`SqlResource`/`SqlService`; the `v1` `resourcey.list` and `resourcey.mongo`
packages have been **deleted** in favour of their `v2` counterparts. The `v2`
backends are `v2/sql` (see below), `v2/mongo` (issue #80), and `v2/list`
(issue #116). Storage-agnostic paging/sort/cursor/cache logic for the `v1` SQL
path lives in `src/resourcey/resource/paged_service.py` (`PagedService`) — a
new backend subclasses it and implements only its data access, never a copy of
the cursor or sort-validation code.

`v2/list`'s `ListResource` is **read-only**: it narrows `actions` to
`read`/`search`/`count`/`batch_read` so no write route is ever mounted. It is
installed with the models it serves (`ListResource(models=[...])`) and the
wrapped Pydantic model is projected onto a `DTO` declaration — there is no
schema generation, no create/update model, and no columns. It is **defensive**
by default: every object it outputs is a deep copy of the stored object, so a
caller cannot mutate the served collection through a result. The list *is* the
storage, delivered through the same service seam; there is no table and no
migration.

The `v1` manifest is declared with resource **instances**, not types:

```python
manifest = ResourceManifest(resources=(Thread(), Message()))
app = manifest.create_app()
```

Because instances carry their own configuration, a resource needing per-app
inputs is simply constructed with them — that is how a `ListResource` gets its
data (`ListResource(countries)`), and how a caller can override a hook per
instance.

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
  never churns the derived models. A `SecretStr` field in a generated model
  gains the secret serializer / validator (`_secret_base`, issue #118), so it
  redacts by default, reveals under `expose_secrets`, and encrypts under an
  `encryption_service` — uniformly across every generated REST shape.
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
  `v2/core` keeps no behaviour and a backend supplies the whole contract. It is
  generic over the DTO `T` and the identifier type `K` (`Resource[T, K]`), so
  `get_service` returns a `Service[T, K]` and the id surface is typed by `K`
  rather than `Any`. The abstract surface is the DTO / REST-model getters
  (`get_dto_type`, `get_rest_models`, `get_id_field`), `get_resource_path`,
  `get_cache_strategy`, the action declaration (`get_supported_actions`, no
  `actions` property) and exposure (`get_exposed_resource`, whose declaration
  wins outright), the service seam (`get_service(ctx)` — **async**, takes an
  optional call-scoped `MutableMapping`, returns a `Service` that is the async
  CM, so the call site is `async with await get_service(ctx)`), registration
  (`on_register` / `get_manifest`), and the lifecycle (`__aenter__` /
  `__aexit__`). Core stays free of storage *and* transport: the per-request
  FastAPI dependency is built in `v2/http`, not here.
* `service.py` — `Service` is generic over the DTO `T` and the identifier type
  `K`, declares the eight actions, and *is* the async context manager; a call
  before `__aenter__` raises. `search` / `count` take a standard `SearchFilter`
  tree (and `search` a `SortOrder`) as plain arguments, not a request object.
  `batch_edit` takes a list of `Edit` nodes — a `kind`-discriminated union of
  `Create[T]` / `Update[T]` / `Delete[K]` — so one batch can create, update,
  *and* delete. It carries no storage: session-per-service and
  session-per-operation are both expressible and core privileges neither. The
  shared rule is *whoever opens the storage owns its commit and close; a
  resource that finds storage already in `ctx` reuses it and neither commits nor
  closes it*. It also carries the `serialization_context()` seam (issue #118):
  `None` by default (redacting), a service overrides it (e.g.
  `{"expose_secrets": True}`) for the one response that should reveal a secret,
  and the transport threads it through `model_dump` / `model_validate`.
* `manifest.py` — `Manifest` owns the resource set and lifecycle, and asserts
  at construction that every `get_supported_actions()` names only real
  `Action` members (a typo would otherwise silently drop a route). Construction
  also calls `resource.on_register(self)` on every resource (declaration order),
  handing each a reference to the manifest; `Resource.on_register(manifest)` is
  **sync** (never a coroutine) and only retains the reference, which
  `Resource.get_manifest()` reads back (`None` until registered). Sibling
  resources are resolved *lazily, later* through that reference (e.g. to verify
  foreign keys) — never from inside `on_register`, since registration ordering
  is not a contract. `Manifest(resources, managers=...)` also takes app-lifecycle
  async context managers (e.g. a `SqlSessionManager`): they are entered **before**
  the resources and exited **after** them, so a resource can still use a manager
  while shutting down. The slot is deliberately generic (`AbstractAsyncContextManager`)
  rather than sql-typed, since `v2/core` must not import `v2/sql`.

HTTP construction (`create_app`) is **not** part of `v2/core` — it belongs to
the transport layer.

### `v2/sql` — the SQLAlchemy backend (issues #78 / #89 / #74)

`src/resourcey/v2/sql/` is the SQL backend on top of `v2/core`, laid out like
`core` (flat files by role, no `__init__.py`). The workflow is **model-first**:
a developer defines the SQLAlchemy ORM model they already work with, and the
framework infers the DTO from it. SQLAlchemy is the schema of record, so
migrations and foreign-key relations stay SQLAlchemy's / Alembic's concern and
a developer can drop straight back to SQLAlchemy.

* `sql_resource.py` — `SqlResource` (a `Resource` subclass) is handed the
  **SQLAlchemy model** it serves and a session source. It infers the DTO (and
  hence the REST models) from the model via `sqlalchemy_2_dto`. There is **no**
  DTO-to-model generation and no declarative base to manage; `model` / `table` /
  `metadata` / `id_column` properties are the escape hatches back to SQLAlchemy.
  The session source is one of: an explicit `session_factory=` (the escape
  hatch, which wins), or a `session_manager=` plus `name=` connection to resolve
  from — defaulting to the process-wide `get_sql_session_manager()` and its
  first connection. Because resolving a connection is async,
  `SqlResource.get_service` is **async**. The file is named `sql_resource.py`
  (and `sql_service.py`) so it is not confused with `v2/core/resource.py` /
  `v2/core/service.py`.
* `sql_service.py` — `SqlService` holds the call-scoped `ctx` and the session
  factory and implements the eight actions. `search` does keyset cursor
  pagination ordered by the identifier, or by a validated `sort` field (with
  the identifier as a stable tie-breaker) when one is requested; `search_filter`
  is pushed into the `WHERE` clause before the page is taken. `batch_edit`
  dispatches over the `Edit` union: a `Create` yields the new DTO, an `Update`
  the updated one, and a `Delete` (or an absent id) yields `None`.
* `sql_config.py` / `db_config.py` — `SqlConfig` (a `BaseConfig`) holds
  `sql_connections: list[DbConfig]`, parsed under the process-wide prefix as
  `APP_SQL_CONNECTIONS_<n>_NAME` / `_URL` / `_PASSWORD`. `DbConfig` is a plain
  nested `BaseModel` (`name` required, plus `url` and an optional `SecretStr`
  `password` spliced in by the `database_url` property via SQLAlchemy's URL
  parser). Blank or duplicate names are rejected at config build
  (`ResourceyConfigError`); lookup is exact/case-sensitive. The field is
  `sql_connections`, not `connections`, so a future non-SQL config can use the
  latter.
* `session_manager.py` — `SqlSessionManager(config)` hands out a session
  *maker* per connection, building each engine lazily and disposing every one it
  built on `__aexit__`; `__aenter__` is an idempotent marker. A lookup with no
  name uses the first connection; an unknown name or an empty list raises
  `ResourceyConfigError`. Using the manager un-entered raises, so a caller who
  forgets `async with manifest` fails loudly instead of leaking engines.
  `get_sql_session_manager()` / `clear_sql_session_manager_cache()` are the
  process-wide accessor and its test-only reset. There is deliberately **no**
  `get_session` — opening a session stays with the code that owns the
  transaction, so the "whoever opens the storage owns its commit and close" rule
  remains the only ownership story.
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
* `cursor.py` — the SQL keyset `WHERE` predicate (`keyset_predicate`); it
  re-exports the storage-agnostic cursor *codec* (`encode_cursor` /
  `decode_cursor`) from `v2/util/cursor.py` so the tamper-proof encoding is
  shared with the non-SQL backends while SQLAlchemy stays out of them. The
  cursor is a JWE from `v2/encryption`.

There is no `v2/sql/migration.py`: with SQLAlchemy as the schema of record,
migrations are delegated to SQLAlchemy and Alembic rather than generated from
the framework's own in-memory models.

### `v2/list` — the read-only list backend (issue #116)

`src/resourcey/v2/list/` is a `v2` backend alongside `v2/sql`: a resource built
from an application-supplied list of Pydantic models and served **read-only**,
for in-process reference data (country codes, feature flags, catalog entries)
that should be exposed over the same REST surface without being copied into a
table. The list *is* the storage — no table, no migration, no external
dependency — which makes it the simplest backend and the proof that a new
backend subclasses `Resource`/`Service` and inherits the rest.

* `list_resource.py` — `ListResource(models, *, model=None, dto=None, path=None,
  defensive=True)`. The served Pydantic model is projected onto a `DTO`
  declaration (via `pydantic_2_dto`); an explicit `dto=` wins (the escape
  hatch). It holds the list **by reference**, mixes in
  `DefaultCacheStrategyMixin` (a read-only resource therefore resolves to
  `OptimisticCacheStrategy(expire_in=600, private=True)` with no per-backend
  code), and narrows `get_supported_actions()` to exactly
  `{read, search, count, batch_read}`. Because `v2/http/routes.py` mounts a
  route only for a declared action, **no write route exists** — a write is a
  `405`, not an unimplemented handler. `defensive=True` deep-copies every
  output (`model_copy(deep=True)`); `defensive=False` serves the stored object
  itself. The query / sort surface derives from `read_response`, the same
  security gate as SQL / Mongo. `get_service(ctx)` is async and adopts items
  seeded on `ctx` under `STORAGE_KEY`, else the resource's own list.
* `list_service.py` — `ListService` implements the read subset over the
  resolved items: `read` (a miss raises `NotFoundError` → 404), `search`
  (filters via `SearchFilter.matches`, orders via `SortOrder.compare` with the
  identifier appended as a tie-breaker, then keyset-pages), `count`, and
  `batch_read` (positionally aligned, `None` for a miss). Paging reuses the
  shared `v2/util/cursor.py` codec and mirrors the SQL `keyset_predicate` in
  memory (NULLs first ascending / last descending; only the sort-key comparison
  mirrors for descending); a cursor reused under a different `(sort_field,
  ascending)` is rejected (`InvalidInputError` → 400). Write methods keep the
  raising `Service` defaults and are never routed.
* `pydantic_2_dto.py` — `pydantic_2_dto(model)` infers a `DTO` declaration from
  a plain Pydantic model, mirroring `sqlalchemy_2_dto`: field order and
  nullability carry across, the identifier is `id` (or an explicit
  `id_field_name=`), and an explicit `DtoField` (an `Annotated` tag or
  `Field(json_schema_extra={"dto_field": ...})`) is honoured verbatim. Other
  fields are left bare so the `v2/core` conventions apply; a read-only resource
  never exercises the create path, so those defaults are simply unused.

### `v2/encryption` — the migrated encryption service (issue #78)

`src/resourcey/v2/encryption/` mirrors v1's layout and holds the migrated
`EncryptionService` (`encrypt_value` / `decrypt_value`, the cursor path, plus
`create_jwe_token` / `decrypt_jwe_token`, the auth-token path) and the key
config (`EncryptionKeysConfig` / `EncryptionKeyConfig`, with the
`encryption_key` + `decryption_keys` rotation model and the `kid` header). The
service itself is built from an **injected config object** and is *not* a
singleton; the env-driven *edge* is a module-level lookup
(`get_encryption_service()` / `clear_encryption_service_cache()`), symmetric
with `get_sql_session_manager()`. `EncryptionKeysConfig` is a `BaseConfig`
(issue #111) parsing `APP_ENCRYPTION_KEY_ID` / `_VALUE` and
`APP_DECRYPTION_KEYS_<n>_*`, so `SqlResource` resolves the service at
construction and cursor pagination works without a caller wiring one in; the
`encryption_service=` argument remains the escape hatch, and
`EncryptionKeyConfig` stays a nested `BaseModel` (mirroring `DbConfig` under
`SqlConfig`). An absent key degrades to a loud dev default
(`EncryptionKeyConfig(value="changeme")` plus a warning) instead of a build
failure; a partially specified key (an id with no value) stays a hard
`ResourceyConfigError`. It sits in its own package (not `core`, which stays
crypto-free, and not `sql`, so a future `v2` auth can use it without reaching
into `sql`).

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
  well as authorization. `OpenDependencyBuilder` is the default: it applies
  **no** authentication or authorization, building the resource's own service
  over the request-scoped `ctx` and yielding it — so its name flags an
  intentionally public app rather than masking a forgotten builder. The
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
  registers one route per supported action, through the `_route` no-clobber
  escape hatch (a developer's route wins). The default OpenAPI tag is the
  resource path humanized (`threads` -> `Threads`, `api-keys` -> `Api Keys`),
  **not** `type(exposed).__name__`: the model-first workflow instantiates a
  generic base directly (`SqlResource(Thread, ...)`), so the class name would
  put every resource in one `SqlResource` bucket and collapse every
  `ResourceView` into `ResourceView`. `tags=` still overrides it, and
  `create_app` / `add_to_app` thread it through. The builder is resolved on the
  **exposed** resource, so a projection's
  wrapped service is the projection's. Every route is registered with a
  per-action `summary` / `description` (from `_operation_metadata`, via the
  exposed resource's DTO name with any trailing `DTO` suffix dropped) — the
  handlers are all closures named `handler`, so without this FastAPI would
  label every operation the bare `"Handler"` with no description. It also holds
  `register_error_handlers`, the projection helper, and the batch-edit body
  union — which is narrowed to the `Create` / `Delete` actions the resource
  declares, so a batch cannot reach an action the declaration omits.

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
pydantic→422 (kept by FastAPI) — and #83 extends the same function. The
service's `serialization_context()` (issue #118) is threaded into
`_project` / `_dump` / `_header_for`, so the wire body (and the ETag that
hashes it) reflects the secret-serialization convention rather than always
redacting.

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
builds `(sort_key, id) > (cursor_key, cursor_id)` for ascending and the mirrored
`(sort_key, id) < (cursor_key, cursor_id)` for descending — but it mirrors only
the sort-key comparison, never the identifier tie-breaker (`ORDER BY key DESC,
id ASC`), so equal keys step to a *greater* id instead of skipping the row. A
`None` cursor key is handled with explicit NULL tests (NULLs sort first
ascending, last descending — `SqlSortConverter` emits `NULLS FIRST` / `NULLS
LAST`), not a `= NULL` comparison. The predicate collapses to a single id
comparison when the sort column *is* the id column. `SqlService.search` validates
the sort field, orders by it, and **rejects** a cursor whose `(sort_field,
ascending)` does not match the current request (`InvalidInputError` → 400) rather
than applying the key against the wrong column; changing the sort of a paged
request therefore means starting a new search (no cursor), matching `v1`. A
malformed cursor surfaces as `InvalidInputError` (400), not a 500.

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
cursor in the ascending order; (3) descending keeps exactly the rows after the
cursor in the *descending* order — key descending, equal keys by ascending
identifier (the tie-breaker never mirrors); and (4) a cursor is accepted only
under the `(sort_field, ascending)` it was built for. The single-attribute model
is unrolled over a finite row universe, as `filtering.qnt` unrolls its tree.

### `v2/cache` — the migrated cache surface (issue #92)

`src/resourcey/v2/cache/` mirrors the old `resourcey.cache` layout (no
`__init__.py`):

* `cache_header.py` — `CacheHeader` (the `etag` / `updated_at` / `expire_at` /
  `private` value object and the `is_modified` matrix). `private` marks a
  caller-scoped freshness window so the HTTP layer emits `Cache-Control:
  private` and a shared cache will not store the response. HTTP-agnostic.
* `cache_strategy.py` — the concrete `CacheStrategy` base plus
  `ETagCacheStrategy` / `LastModifiedCacheStrategy` / `OptimisticCacheStrategy`.
  The concrete base is a `DiscriminatedUnionMixin` **and** extends
  `resourcey.v2.core.service.CacheStrategy` (the placeholder `v2/core` names),
  so a strategy satisfies the core-level seam while its behaviour lives in
  `cache`. `get_cache_header(items, *, context=...)` hashes the read-model
  projection; `count_cache_header(count, filters)` handles the bare-integer
  `count` route (a count-derived ETag; never last-modified). The optimistic
  strategy overrides `count_cache_header` to return freshness only, so it emits
  no validator on **any** route — the read and count routes agree.
* `cache_defaults.py` — the storage-agnostic default policy.
  `default_cache_strategy(rest_models, supported_actions)` selects, in order: a
  **read-only** resource (one advertising none of the write actions) →
  `OptimisticCacheStrategy(expire_in=DEFAULT_READ_ONLY_EXPIRE_IN, private=True)`
  (600s), since a surface that cannot change gains nothing from a validator; else
  last-modified when the read model carries `updated_at`; else ETag. The
  read-only window is **always `private`**: read-only does not imply the same
  bytes for every caller (a permission-narrowed search is caller-scoped), and
  the API-key header is not `Authorization`, so RFC 9111's authenticated-response
  protection does not apply — a shared cache must be told not to store it.
  `is_read_only(actions)` is the write-action test. `DefaultCacheStrategyMixin`
  packages the default `get_cache_strategy()` — including the per-instance
  caching — so any backend inherits the policy by supplying only
  `get_rest_models()` and `get_supported_actions()` rather than copying it.
  Those two hooks stay `@abstractmethod` in the mixin so it does not, by defining
  concrete overrides, drop them from `Resource`'s abstract set (a backend that
  forgets one still fails at instantiation).

The wiring: `Resource.get_cache_strategy()` returns `None` in `v2/core` (which
stays dependency-free); `SqlResource` mixes in `DefaultCacheStrategyMixin` and
so resolves the default **per instance** (one `SqlResource` class serves many
DTOs, so a class-level cache would leak between them) and caches it on the
instance. A developer overrides the same hook to change the policy.
`v2/http/routes.py` reads the exposed resource's strategy once, passes it to
every route builder, and each handler emits `ETag` / `Last-Modified` /
`Cache-Control` / `Expires` and short-circuits a conditional `GET`/`HEAD` to
`304 Not Modified`; a validator with no freshness window forces revalidation
with `Cache-Control: no-cache`, and a `private` header prefixes `private` onto
the freshness directive. `specs/cache_defaults.qnt` pins the selection and the
`private` scoping.
Only the projected REST representation is hashed, so the ETag validates exactly
the bytes sent.

### `v2/mongo` — the MongoDB backend (issue #80)

`src/resourcey/v2/mongo/` is the non-SQL proof of the `v2` seams, laid out like
`sql` (flat files by role, no `__init__.py`) and sitting at the same layer rank.
The workflow is **DTO-first**: Mongo has no schema of record, so the developer
declares a `DTO` and hands it to the resource —
`MongoResource(ThreadDTO, name="main")`. The DTO declaration drives the six REST
models, the identifier, and the query surface (the read model *is* the filter /
sort surface — the same security gate `SqlResource` uses). A bare `id: UUID`
gets a server-side `uuid4` create default from the `v2/core` conventions, so
`v1`'s `_make_id_optional` hack is gone, and `created_at` / `updated_at` are
framework-owned.

* `mongo_resource.py` — `MongoResource` (a `Resource` subclass) serves a DTO
  declaration. The session source mirrors `SqlResource`'s escape hatches: an
  explicit `client=` (or a `ctx`-preseeded client) wins; otherwise the collection
  is resolved from `client_manager` by `name`, defaulting to the process-wide
  `get_mongo_client_manager()` and its first connection. Because resolving a
  connection is async, `get_service` is async. `get_resource_path` / `get_id_field`
  derive from the DTO, and `get_queryable_fields` / `get_filter_operators` /
  `get_sortable_fields` derive from the read model (a field projected away is
  neither filterable nor sortable). `get_search_filter_type()` /
  `get_sort_order_type()` return `None` (derive) by default, and
  `resolve_sort_order(sort, desc)` validates against the sortable surface
  (`InvalidInputError` → 400). `DefaultCacheStrategyMixin` supplies the shared
  cache default. Indexes are opt-in (`get_indexes()` / `ensure_indexes()`, run
  from `__aenter__`) and `migrate_document(doc) -> doc` is the manual
  migration-on-read hook (default no-op) invoked on every read path. `__aexit__`
  drops the cached collection (the manager closes its clients on exit), so a
  re-entered lifecycle re-resolves from the rebuilt client rather than handing
  out a collection bound to the closed one.
* `mongo_service.py` — `MongoService` implements the eight actions against a
  duck-typed async collection (`insert_one`, `find_one`, `find`,
  `find_one_and_update`, `delete_one`, `count_documents`, `create_index`) so
  `mongomock` substitutes for `motor`. `create` / `update` apply the DTO's
  create / update defaults (through the shared `apply_operation_defaults`), a
  UUID is stored as its string under `_id`, and a miss raises `NotFoundError`.
  A duplicate key (`insert_one` raising pymongo's `DuplicateKeyError`) is
  translated to `ConflictError` — the storage-neutral 409 — so the transport
  maps it without importing the driver. `search` pushes the filter into `find`,
  appends `_id` ascending as the stable tie-breaker, and pages with the shared
  keyset cursor (`limit + 1` to detect the next page). `batch_edit` dispatches
  over the `Create` / `Update` / `Delete` union, refusing create / delete when
  the resource does not declare them.
* `mongo_filter_converter.py` / `mongo_sort_converter.py` — three registries
  (logical / attribute / operator) and a type-keyed sort registry, each with an
  import-time completeness assert, mirroring the SQL converters. Every operator
  registers a `(positive, negated)` pair whose negated form reproduces the
  in-memory `matches` complement on absent / null fields (`$ne` alone drops
  absent documents, which `EqFilter` on `None` matches). Pushdown is
  all-or-nothing: an unconvertible node raises `UnsupportedFilterError` unless
  the resource sets `allow_filter_iteration`. The sort context carries only
  **sortable** fields, and Mongo's fixed NULL ordering (`NULLS FIRST` ascending,
  `NULLS LAST` descending) must agree with the in-memory `compare` and the
  keyset predicate.
  The match-nothing sentinel is `{"$nor": [{}]}` (never `{"_id": None}`, which
  would match a document whose client-supplied nullable identifier is null).
* `mongo_client.py` / `mongo_config.py` / `embedded.py` — `MongoClientManager`
  is the `SqlSessionManager` analogue: it hands out a client/database per
  connection, built lazily, disposed on `__aexit__`, and entered through
  `Manifest(managers=[...])`. A lookup with no name uses the first connection;
  an unknown name or an empty list raises `ResourceyConfigError`; using it
  un-entered raises. `get_mongo_client_manager()` /
  `clear_mongo_client_manager_cache()` are the process-wide accessor and its
  test-only reset. `MongoConfig` (`BaseConfig`) parses
  `APP_MONGO_CONNECTIONS_<n>_NAME` / `_URL` / `_PASSWORD` (field
  `mongo_connections`, with `connections` as a read-only alias, because the env
  parser derives the variable from the field name); `MongoConnectionConfig` is a
  plain nested `BaseModel` with `is_embedded` / `mongo_database_name` /
  `mongo_password`. Blank or duplicate names are rejected at config build.
  `embedded://<db>` (and the bare `embedded` marker) builds the in-process
  `AsyncEmbeddedClient` over `mongomock`; a `mongodb://` URL builds a real
  `motor` client, with the password passed as a client kwarg **only when set**
  (`password=None` would clear a URL-embedded password).

`mongodb = ["motor>=3.6"]` stays the only place `motor` is required:
`v2/mongo` is import-safe without it, and building a real client without it
fails with an actionable `ImportError` naming `resourcey[mongodb]`. `mongomock`
/ `pymongo` remain dev-only for the embedded path and tests.

### `v2/view` — the configured wrapper (issue #121)

`src/resourcey/v2/view/` holds `ResourceView`, the `v2` successor to `v1`'s
`WrapperResourceBase` (#62), but **configuration-driven** rather than
subclass-driven, so a projection is an instance:

```python
public_secrets = ResourceView(
    resource=secrets,
    exposed_field_overrides={
        "value": {
            "in_read_response": False,
            "in_search_response": False,
            "in_update_request": False,
            "in_update_response": False,
        }
    },
    exposed_actions=frozenset(Action) - {Action.UPDATE},
)
```

It is the general-purpose **least-privilege** tool: declare a resource once with
its full storage truth, then narrow the public surface without touching the
storage class. The premise "delegate everything except `get_exposed_resource`"
is *almost* right — field overrides force the whole exposed surface to be
recomputed, because the query surface and cache policy derive from the DTO /
REST models and the action set:

* **Field projection** — the DTO is re-derived from the inner declaration via a
  new `derive_dto(dto, field_overrides=...)` in `v2/core/dto.py`. It re-declares
  every field with its *resolved* `DtoField` made explicit and merges the
  per-field override onto it with `DtoField.with_overrides`, so the `DTO`
  conventions do **not** re-run (a bare `id` keeps its `uuid4` factory). The
  stored annotation is reduced to its base type before the merged field is
  attached, so the `Annotated[T, DtoField(...)]` (DTO-first) form overrides
  identically to the class-attribute (SQL) form. An unknown field, an override
  of the identifier, or an override that turns an inner-`False` projection flag
  back on is rejected at construction.
* **Query / sort surface** — `get_queryable_fields`, `get_filter_operators`,
  `get_sortable_fields`, and `resolve_sort_order` are recomputed from the view's
  read model. Load-bearing, not cosmetic: delegating them to the inner would let
  `?secret__eq=` / `?sort=secret` pass the transport's validation and reach the
  inner service, which pushes them down against the inner's (wider) column set —
  leaking the hidden value or its relative order. `get_search_filter_type()` is
  carried across only when the view does not narrow; hiding fields while the
  inner declares an object filter is refused at construction (the declared
  filter still names every inner field).
* **Cache policy** — recomputed when the view narrows, so a view that becomes
  read-only selects the optimistic, `private` strategy rather than inheriting a
  writable inner's shared-cacheable validator; a pure pass-through delegates.
  `cache_strategy=` is the escape hatch.
* **Actions** — normalized (`normalize_actions`), and `exposed_actions` must be a
  subset of the inner's: a view **narrows**, never widens.

`v2/core/service.py` gains `normalize_actions(actions)` — a batch action is
dropped when its singular action is absent (`batch_read` needs `read`;
`batch_edit` needs at least one of create / update / delete). It is applied
where actions are consumed: `register_routes` (so routes and the batch body
agree), the SQL / Mongo `batch_edit` (which now guards `Update` like create /
delete), and the view. The `batch-edit` body's `Update` kind is now gated on
`Action.UPDATE` (previously always present), closing a latent hole where a
resource exposing `batch_edit` while hiding `update` could update through the
batch. `specs/exposure.qnt` pins both.

`view_service.py` holds `ViewService`, a `Service` proxy that forwards every
action to the inner but re-asserts the **view's** actions on `batch_edit` —
defense in depth for a direct service caller, since the inner service consults
the inner (wider) action set. The view's `get_service` wraps the inner's, and
`__aenter__` / `__aexit__` / `on_register` delegate to the inner so its
lifecycle (e.g. `MongoResource.ensure_indexes()`) still runs; register the
**view**, not the inner (registering both double-enters the inner and mounts
duplicate routes). The layer ranks gain `view` at the backend rank.

### `v2/filestore` — pre-signed-URL files (issue #117)

`src/resourcey/v2/filestore/` adds **file bytes** as a first-class resource
without putting the bytes on a request path. The *metadata* (name, size, MIME
type, checksum, the medium's ETag, status, opaque storage key) is an ordinary
model-first `SqlResource`, so it gets the standard surface plus cache headers;
the *bytes* live in a pluggable medium behind `FileStore`. The client transfers
directly against a short-lived capability URL and the API only mints it — so
authorization stays in the API while the storage medium (which cannot see the
API's auth) moves the bytes.

* `file_store.py` — `FileStore` (a `DiscriminatedUnionMixin`, `kind` = class
  name) is the medium seam: `put` / `get` / `head` / `delete` (async, the
  server-side fallback) plus `presign_put` / `presign_get` (sync: a native SigV4
  computation for S3, a local JWE mint for SQL / local, neither blocking the
  event loop). `StoredObject` is a `head` result (`key`, `size`, `content_type`,
  `etag`, `updated_at`); `PresignedUrl` is the identical-shape handshake result
  (`url`, `method`, `expires_at`, `headers`). A store is its own async context
  manager, entered through the manifest's `managers=` slot exactly like a
  `SqlSessionManager` / `MongoClientManager`, so its client lifecycle is tied to
  the app.
* `file_metadata.py` — the metadata model + `file_resource(store, ...)`. Two
  guarantees are built in: **`updated_at`** is an ordinary column with
  `default` / `onupdate`, so the DTO conventions make it framework-owned and it
  appears in every response shape; **MIME type** (`content_type`) is a
  first-class column. The cache policy is overridden to a strong **ETag** over
  the projected bytes (the default last-modified would otherwise win, since the
  read model carries `updated_at`). `key` and `status` are server-owned (absent
  from every create / update request and, for `key`, every response); `etag` is
  server-owned. `FileMetadataService` assigns the opaque key + `pending` status
  on create, enforces the optional `max_size` cap (defaulted from
  `FileStoreConfig.max_size`), and on delete removes the row *and* the object so
  no orphan remains.
* `local_file_store.py` — the default medium (single instance, dev, tests):
  bytes under `root`, MIME type in a `.meta` sidecar (the filesystem records
  none), ETag an MD5 of the bytes. Keys are opaque server-assigned hex, never
  client paths: `..`, an absolute path, `~`, and an empty key are rejected and
  the resolved path is re-checked to live under `root`.
* `sql_file_store.py` — bytes in a dedicated `file_blobs` table (no second
  system). The blob table is **storage, not a resource**: never registered and
  never DTO-derived, so bytes cannot leak through a read model. The app owns the
  schema (`create_blob_tables` for `create_all`, or Alembic against
  `FileBlobBase`). The session source mirrors `SqlResource` (explicit
  `session_factory=` wins; else resolve from `session_manager=` by
  `connection_name`, defaulting to the process-wide manager).
* `s3_file_store.py` — the production medium: native SigV4 pre-signed URLs
  (`presign_*` is local, no network round trip) and client put/get/head/delete.
  `boto3` is imported **lazily**, only when a real client is built (an explicit
  `client=` is the escape hatch), so `v2/filestore` imports without the extra;
  the optional dependency is `s3` (`resourcey[s3]`), and a single-`PUT` cap
  applies (`S3_MAX_PUT_BYTES`; multipart is out of scope).
* `signed_url.py` — the framework-signed capability for the SQL / local
  mediums: `mint_signed_url` produces a JWE over `v2/encryption` carrying
  `{"k": key, "op": "put"|"get"}` plus `iat` / `exp`, and `verify_signed_url`
  rejects (400) a malformed / tampered token, an expired one (the codec does
  **not** enforce `exp`, so the caller must), one presented for the wrong
  operation, and one missing its key. The token is a **bearer capability**: it
  is short-lived and bound to exactly one `(key, op)` pair, and the route
  rejects a token for key A used on key B. `SignedFileStore` owns the mint /
  verify helpers over an injected `EncryptionService` so a concrete medium only
  implements the medium operations.
* `file_config.py` — `FileStoreConfig` (a `BaseConfig` block): the medium is
  selected with no code change through a `LazyField` (`MEDIUM_CLASS` names a
  `FileStore` subclass; unset ⇒ `LocalFileStore`), and the selected medium's own
  fields parse under the `MEDIUM_` prefix. TTLs / cap are
  `APP_UPLOAD_URL_TTL_SECONDS` / `APP_DOWNLOAD_URL_TTL_SECONDS` / `APP_MAX_SIZE`
  (the `*_seconds` spelling keeps them env-parseable; `*_ttl` properties expose
  `timedelta`).
* `file_routes.py` — `register_file_routes(app, store, resource=files, ...)`,
  called after `create_app`. It adds **no** standard `Action` member — a presign
  handshake is genuinely not one of the eight — and mounts the capability
  transfer endpoints (`PUT` / `GET` `/_files/{key}`, hidden from the schema) plus
  the metadata handshake: `POST {resource}/{id}/upload-url` mints a `put`,
  `POST {resource}/{id}/complete` heads + verifies + flips to `ready` (and
  records the ETag), `GET {resource}/{id}/download` mints a `get` for a `ready`
  file. Minting is authorized through the resource's normal
  `DependencyBuilder` seam, so a caller must be permitted to act on the file
  before receiving a URL.

`specs/filestore.qnt` pins the handshake: `create → upload → complete → ready`,
the completion guards (object present, size matches, not already ready),
`download` requires `ready`, `delete` removes the object, capability binding,
distinct keys, and the `ready`-implies-uploaded / ETag-matches-medium /
`pending`-has-no-ETag invariants. It is part of `make specs` and CI.

### `v2/` isolation

`v2/core`, `v2/sql`, `v2/mongo`, `v2/list`, `v2/view`, `v2/filestore`,
`v2/auth`, `v2/encryption`, `v2/util`, `v2/config`, `v2/cache`, and `v2/http`
are **parallel** to the existing packages — nothing
existing is removed by them and they are not a refactor. The old `v1`
packages/modules (and the old `resourcey.encryption`) stay in place until a
follow-up removal. A test asserts that no module under `v2/` makes a **runtime**
import of any `resourcey` code *outside* `v2/` (a static AST walk covering every
v2 layer in one rule), `if TYPE_CHECKING:` imports still allowed. A second test
pins the **layer ranks**
`util < core < {sql, mongo, list, view, filestore, http, config, cache, encryption, auth}`:
no module imports a strictly-higher project layer at runtime. `v2/sql`, `v2/mongo`,
and `v2/list` implement whatever small helpers they need locally rather than
reaching for `resourcey.util`.

### `v2/util` and `v2/config` — the config rung

`src/resourcey/v2/util/` (issue #82) is the **bottom layer** of `v2` and where
the dependency-free vendored leaves now live: `models.py`
(`DiscriminatedUnionMixin`), `import_paths.py` (dotted-path resolution),
`env_parser.py`, and `missing.py` (the `Missing` / `MISSING` sentinel, moved
here by issue #86), plus the non-vendored `cursor.py` (the storage-agnostic
keyset cursor codec, extracted from `v2/sql` by issue #116) and the shared
`naming.py` / `singleton.py` / `search_filter.py` / `sort_order.py` leaves.
They are copies, not moves — v1 `resourcey/util/` is untouched until it is
removed. `v2/util` imports **no project package** at all (not even `v2/core`),
so the layer ranks are a clean

    util < core < {sql, mongo, list, view, filestore, http, config, cache, encryption, auth}

and `v2/core` may import `v2/util` — the dependency runs one way.

`src/resourcey/v2/util/secret_serialization.py` (issue #118) is the `v2` port of
v1's context-driven secret convention: `dump_secret_str` / `load_secret_str` read
an `encryption_service` (encrypt / decrypt at the storage boundary) or
`expose_secrets` (plaintext) from the pydantic context, and otherwise **redact**;
encryption wins over exposure. It is wired onto generated models by
`v2/core/dto.py` (`_secret_base`, so every generated REST shape carries the
serializer / validator), a service supplies its context through
`Service.serialization_context()` in `v2/core/service.py`, and
`v2/http/routes.py` threads it through `_project` / `_dump`.
`specs/secret_serialization.qnt` pins the precedence.

`src/resourcey/v2/util/cursor.py` (issue #80) is the storage-agnostic half of
the pagination cursor: the tamper-proof, type-tagged codec
(`encode_cursor` / `decode_cursor` over the JWE from `v2/encryption`) that both
`v2/sql` and `v2/mongo` share. `keyset_predicate` — the ``WHERE`` clause — stays
in `v2/sql/cursor.py` because it imports SQLAlchemy, and that module re-exports
the codec so existing `resourcey.v2.sql.cursor` importers keep working; `v2/mongo`
builds its own Mongo keyset query.

`src/resourcey/v2/util/naming.py` holds the shared name helpers `camel_to_kebab`
(inserts `-` boundaries without lowercasing), `camel_to_snake` (the same
boundaries with `_`), `pluralize` (a small `s` / `es` rule preserving case), and
`humanize` (camel/kebab/snake identifier → Title Case words, e.g. `api-keys` →
`Api Keys`), public and reusable by any backend — e.g. `SqlResource` composes
`camel_to_kebab` + `pluralize` for its default `get_resource_path`,
`MongoResource` composes `camel_to_snake` + `pluralize` for its collection name,
and `v2/http/routes.py` composes `humanize` + `pluralize` for its default
OpenAPI tag. They were formerly private to `v2/core/resource.py`.

`src/resourcey/v2/util/singleton.py` (issue #95) is a second, non-vendored
leaf: a small `Singleton` mixin for the process-wide pieces the framework
keeps accruing (a dependency builder, a cache, the search-filter leaves like
`AllFilter` / `NoMatchFilter`). Constructing
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
`v2/config/lazy_field.py`, and `v2/sql/sql_service.py` import it and there is no
re-export. A test pins `env_parser.MISSING is missing.MISSING` so a future
re-copy of the vendored file cannot quietly reintroduce a second sentinel.

`src/resourcey/v2/config/` ships the generic machinery only: `config_base.py`
and `lazy_field.py`. There is **no** `config_loader.py` — `v2` does no `.env`
loading of its own (`get_instance()` reads `os.environ` only; use
`uvicorn --env-file` or a wrapper script). `FrameworkConfig`, `MigrationConfig`,
`AuthConfig`, `IdpConfig`, and `DependencyBuilder` are deferred to a later PR,
so there is no `config_framework.py` / `config_dependency.py` here yet, and no
`config_runtime` at all (`v2` gains no `RESOURCEY_CONFIG_CLASS` selector). The
framework config *blocks* that do exist live with what they configure
(`SqlConfig` in `v2/sql/`, `EncryptionKeysConfig` in `v2/encryption/`), and an
app composes them by inheritance — `class AppConfig(SqlConfig,
EncryptionKeysConfig)` — so one `AppConfig.get_instance()` exposes every block
and `generate_env_template()` covers them all. The app calls
`AppConfig.get_instance()` at its entry point; framework internals keep
resolving their own block, and the per-class caches stay independent while the
values agree because all read the one env namespace.

`BaseConfig.get_instance()` caches **per class** and is typed to the owning
class: `MyAppConfig.get_instance()` returns a `MyAppConfig`,
`FrameworkConfig.get_instance()` returns a `FrameworkConfig`. There is no
super/subclass acceptance check and therefore no "not a subclass" error, and
`clear_instance_cache()` clears only the class it is called on — a base and a
subclass cache independently. The environment is the single source of truth:
**one process-wide prefix** (`get_config_prefix` / `set_config_prefix`, default
`APP`) governs every class, so an app config extends a framework config without
the framework needing to know the app's fields. The first read **latches** — a
later `set_config_prefix` raises `ResourceyConfigError` (and clears every
class's instance cache) because a late set would silently reuse configs built
under the old prefix; `_reset_config_prefix` is the test-only reset. Because
all classes share one flat namespace, `BaseConfig.__init_subclass__` rejects —
with a `TypeError` at class creation — a field name declared by two classes
with **different** types, while a same-name/same-type redeclaration is allowed;
`ClassVar` entries (`LazyField`) are not fields.
`ResourceyConfigError` (with `ResourceyError`) lives in `v2/core/errors.py` and
covers build/parse failures only; `ServiceError` / `NotFoundError` stay in
`v2/core/service.py`. `InvalidInputError` / `UnsupportedFilterError` /
`ConflictError` also live there — the storage-neutral errors a backend raises and
the transport maps (`ConflictError` is what a backend's duplicate-key failure
becomes, so the 409 mapping needs no driver import).

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
