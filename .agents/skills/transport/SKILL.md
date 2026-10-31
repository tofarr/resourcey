---
name: transport
description: HTTP assembly — create_app / add_to_app, the DependencyBuilder seam, register_routes and the action-to-route mapping, the error envelope, and cache header emission. Load when changing routes, app wiring, or the request path.
version: "2.0.0"
---

# The transport layer

`src/resourcey/http/` holds HTTP assembly as **free functions**, not methods on
`Manifest` (which stays a plain container), with no lazy imports. It is the only
layer that knows FastAPI; `core` must not import it.

## App assembly

```python
create_app(manifest, *, cors_origins=None, dependency_builder=None, tags=None)
add_to_app(manifest, app, *, prefix="/", dependency_builder=None, tags=None)
```

`create_app` builds a fresh FastAPI whose lifespan is `async with manifest`, then
mounts routes + error handlers + CORS. `add_to_app` mounts the same onto a
user-owned app and **does not wire a lifespan** (Starlette has one lifespan slot,
so the caller composes it). Both thread `dependency_builder` through to
`register_routes`.

## The DependencyBuilder seam

`DependencyBuilder` (a `DiscriminatedUnionMixin`) declares
`get_service_dependency(resource) -> Callable[..., object]` — an ordinary FastAPI
dependency whose author may declare any parameter FastAPI can wire (the
`Request`, other `Depends(...)`, i.e. an auth dependency). So the seam covers
authentication as well as authorization.

* `OpenDependencyBuilder` is the default: **no** authentication or
  authorization. Its name flags an intentionally public app rather than masking a
  forgotten builder.
* `get_principal_dependency()` optionally returns a dependency the transport adds
  to every route's `dependencies=[...]`, so an authenticator's OpenAPI security
  scheme is declared without changing the route signature. It guards the route
  without becoming a declared parameter, and the authenticated result is cached
  per request, so there is no double authentication.
* `request_ctx(request)` owns the call-scoped mapping on `request.state`
  (key `resourcey_ctx`), shared by every resource in one request — which is what
  makes the storage-ownership rule work.

The seam is **authorization only** — it cannot add or remove routes (that is
`get_exposed_resource()`'s sole call), and a restrictive posture returns a
dependency that *denies* rather than nothing, keeping the route visible in
OpenAPI. `register_routes` calls it **once** at registration and asserts the
result is callable, so a misconfigured builder fails at startup, not per
request.

Builders compose: `AuthorizedDependencyBuilder` (auth + policy) and
`TriggeredDependencyBuilder` (triggers, wrapping an `inner` builder) both take
the same slot, and either order is the app's choice.

## register_routes

```python
register_routes(app_or_router, resource, *, prefix="", tags=None,
                dependency_builder=None)
```

* Resolves the **exposed** resource once (`get_exposed_resource()`), so a
  projection's wrapped service is the projection's.
* Registers one route per **normalized** supported action — no route for an
  action the resource does not declare, so a write on a read-only resource is a
  `405`.
* Static sub-paths (`/count`, `/batch-read`) are registered
  before the `{id}` routes, or `batch-read` would be captured as an id.
* The default OpenAPI tag is the resource path humanized (`threads` → `Threads`,
  `api-keys` → `Api Keys`), **not** the class name: the model-first workflow
  instantiates a generic base directly, so the class name would collapse every
  resource into one `SqlResource` bucket.
* Every route gets a per-action `summary` / `description`; the handlers are all
  closures named `handler`, so without this FastAPI would label every operation
  the bare `"Handler"`.
* `_route(...)` is the **no-clobber escape hatch**: a developer's route
  registered at the same path + method wins. This is how `filestore` replaces
  the generated create route.
* `register_error_handlers(app)` installs the error envelope (below).

The action → shape mapping:

| Action | Method | Path | Request model | Response model | Status |
|---|---|---|---|---|---|
| `create` | POST | `/{resource}` | `create_request` | `create_response` | 201 |
| `read` | GET | `/{resource}/{id}` | — | `read_response` | 200 |
| `update` | PATCH | `/{resource}/{id}` | `update_request` | `update_response` | 200 |
| `delete` | DELETE | `/{resource}/{id}` | — | — | 204 |
| `search` | GET | `/{resource}` | query params | `search_response` page | 200 |
| `count` | GET | `/{resource}/count` | filter query params | bare integer | 200 |
| `batch_read` | GET | `/{resource}/batch-read` | repeated `id` params | list of `search_response` | 200 |

`create` maps to `create_response` (not read) so a one-time-reveal field
survives. Services return DTOs; the transport **projects** them onto the REST
model, dropping `MISSING`.

There is no per-resource batch write: `POST /batch-edit` (below) is the only
one (#179). Its body is a `kind`-discriminated union narrowed to the actions the
resource declares: a resource without `create` / `delete` admits only `Update`.
The condition field is typed `Any` with an explicit JSON-schema override and a
`mode="before"` resolver — typing it as the parameterised `SearchFilter`
generic breaks FastAPI's OpenAPI generation.

## Query surface

`search` takes `limit`, `cursor`, `sort`, `desc`, and one typed `Query` param per
`<attribute>__<op>` synthesised from the resource's filter surface (so they
appear in OpenAPI and are coerced). `_resolve_filters` rejects any unknown
`field__op` key with `InvalidInputError` (400) — FastAPI silently ignores
unknown query params, so a typo must not be dropped — and rejects any filter
param at all when the resource declares no surface. The count route's ETag
hashes the resolved filter too, so distinct filters get distinct validators.

## Request caps

Three caller-controlled knobs are capped at the transport, from
`RequestLimitsConfig` (`src/resourcey/http/http_config.py`), read **once** at
registration and threaded into the route builders:

| Config field | Env var | Default | Bounds |
|---|---|---|---|
| `max_search_limit` | `APP_MAX_SEARCH_LIMIT` | 100 | an explicit `search` `limit` |
| `max_batch_read_ids` | `APP_MAX_BATCH_READ_IDS` | 100 | the id count in one `batch_read` |
| `max_batch_edits` | `APP_MAX_BATCH_EDITS` | 100 | the node count in one app-level `batch_edit` |

* All three must be `>= 1`; `RequestLimitsConfig.get_instance()` raises
  `ResourceyConfigError` otherwise. `None` is not accepted — a silently
  unbounded surface is the footgun the caps close. In particular
  `register_batch_routes(max_edits=None)` (the default) means "read the config",
  **not** "uncapped"; pass an int to override for one mount.
* The caps live in `config`, which `core` (rank 1) cannot import, so enforcement
  is here, not in `Service`. A **direct service call is trusted code and is not
  capped** — the same split that has the transport validate the sort/filter
  surface.
* `DEFAULT_LIMIT` (20, `core/service.py`) is the *page size used when the client
  omits `limit`*. It is not the cap. The route's `limit` default is
  `min(DEFAULT_LIMIT, max_search_limit)` resolved **at registration**, so an
  omitted `limit` is always valid, even under a cap below 20. Only an *explicit*
  over-cap value is rejected.
* A `limit` below 1 is rejected too: SQL would read `-1` as `limit(0)` and
  return an empty page echoing a nonsense `limit=-1`.
* An over-cap `batch_read` is **rejected, never truncated** — the result is
  positionally aligned with the input, so a shortened list would misalign every
  position after the cut (and break the `DataLoader` contract).
* Both rejections are `400 invalid_input` through the error envelope, matching
  the other bad-input cases. Because the checks are manual
  (`_check_search_limit` / `_check_batch_read_ids`) rather than `le=` on the
  `Query`, the bounds are declared with `json_schema_extra` (`minimum` /
  `maximum` / `maxItems`) so they still appear in the generated OpenAPI
  document.

The app-level `batch_edit` node count is capped by `max_batch_edits` (#177) —
the only batch write route, so every batch is bounded. `MAX_IN_VALUES` (500, for
`?id__in=`) and `ACL_MAX_IDS` (100) are separate, pre-existing caps.

## Error envelope

Every framework error becomes:

```json
{"error": {"code": "not_found", "message": "No entity with id ..."}}
```

| Status | `code` | Raised by |
|---|---|---|
| 400 | `invalid_input` | `InvalidInputError` (bad filter, sort, cursor, over-cap `limit` / `batch_read` / batch node count) |
| 401 | FastAPI default (`{"detail": ...}`) | the strict/lenient principal dependency, with `WWW-Authenticate` |
| 403 | `forbidden` | `ForbiddenError` (denied create) |
| 404 | `not_found` | `NotFoundError` (absent, or out-of-scope by id) |
| 409 | `conflict` | `ConflictError` / SQLAlchemy `IntegrityError` |
| 422 | FastAPI default | pydantic request validation |
| 500 | `internal_error` | `ServiceError` |
| 501 | `unsupported_filter` | `UnsupportedFilterError` |

A 401 is raised by FastAPI's `HTTPException`, so it carries `{"detail": ...}`
rather than the framework envelope — that is deliberate (it is the standard
`WWW-Authenticate` path), but tests should assert on the status and header, not
on `error.code`.

## Cache headers

The exposed resource's strategy is read once and passed to every route builder.
Each handler emits `ETag` / `Last-Modified` / `Cache-Control` / `Expires` and
short-circuits a conditional `GET` / `HEAD` to `304 Not Modified`. A validator
with no freshness window forces revalidation with `Cache-Control: no-cache`; a
`private` header prefixes `private` onto the freshness directive. When the
service reports a caller-scoped response (`response_is_private()` — e.g. an
`Owner`-scoped `AuthorizedService`), the transport forces `private` onto the
strategy, so a shared cache neither stores nor revalidates it.

Only the projected REST representation is hashed, so the ETag validates exactly
the bytes sent. `_project` / `_dump` / `_header_for` all thread the service's
`serialization_context()`.

## register_batch_routes

```python
register_batch_routes(app_or_router, manifest, *, path="/batch-edit", prefix="",
                      dependency_builder=None, max_edits=None, tags=None)
```

The app-level `POST /batch-edit`: one ordered list of create / update / delete
nodes spanning **every** exposed resource, answered positionally. `create_app` /
`add_to_app` mount it by default (`batch_edit=False` opts out,
`batch_edit_path=...` moves it, `batch_edit_max_edits=...` overrides the cap).
`max_edits=None` reads `RequestLimitsConfig.max_batch_edits` — it never lifts
the cap.

* The body is a **static** two-level discriminated union built at registration:
  `resource` → `kind`. Each resource contributes its own union
  (`_resource_edit_body`), so a `kind` the resource does not declare is a `422`.
* Targets are exposed resources that declare at least one write action — the
  gate that replaced the removed `Action.BATCH_EDIT`. A hidden or read-only
  resource contributes nothing, so it is unreachable here.
* Nodes apply in **list order**, never grouped, so a later node sees an earlier
  node's write. Consecutive nodes for one resource share one service (and one
  transaction where the backend has one).
* Every service stays open on an `AsyncExitStack` until the batch finishes:
  closing a run early would commit it and defeat rollback. The first failure
  propagates to the error envelope; rollback is **best effort** and holds where
  the backend provides a shared transaction (SQL on one session factory).
* `build_service` on the `DependencyBuilder` is the seam — the route selects a
  resource at runtime, so it cannot use the static `get_service_dependency`.
  An authorizing builder therefore wraps each node's service, which is what
  keeps this route from being an authorization bypass.

## Escape hatches

`add_to_app` (mount on your own app), `register_routes` (mount one resource on
your own router), `_route`'s no-clobber behaviour (define your route first),
and the `register_*_routes` free functions in `filestore` / `auth` / `realtime`
(mounted after `create_app`, for surfaces that are not one of the eight standard
actions).
