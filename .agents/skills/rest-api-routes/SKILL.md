---
name: rest-api-routes
description: REST API conventions — resource actions, naming, verbs, batch endpoints, and error shapes. Load when designing or modifying REST routes.
version: "1.0.0"
---

# REST API routes

Every generated resource service exposes the **standard actions** over REST:

| Action | Method | Path | Body | Returns |
|---|---|---|---|---|
| `create` | POST | `/{resource}` | create payload | created entity |
| `read` | GET | `/{resource}/{id}` | — | entity |
| `update` | PATCH | `/{resource}/{id}` | partial payload | updated entity |
| `delete` | DELETE | `/{resource}/{id}` | — | 204 |
| `search` | GET | `/{resource}` | query params | page of entities |
| `count` | GET | `/{resource}/count` | filter query params (same as `search`) | bare integer |
| `batch_read` | GET | `/{resource}/batch-read` | repeated `id` query params (`?id=foo&id=bar`) | list of entities |
| `batch_edit` | POST | `/{resource}/batch-edit` | list of edits | list of entities |

## Conventions

* All URL paths use **dashes**, never underscores. The `{resource}` segment
  is plural, lower-case, kebab-case (from `get_resource_path()`); action
  sub-paths are dash-separated too (`batch-read`, `batch-edit`, `count`).
  Python method names stay snake_case (`batch_read`, `batch_edit`) — only
  the URL is dash-separated.
* `batch_read` is a `GET` (a pure read with no side-effects), so its ids are
  passed as repeated `id` query parameters (`?id=1&id=2&id=3`) rather than a
  request body.
* `search` uses **cursor-based (keyset) pagination** with query parameters
  for filtering, sorting, and paging:
  `?limit=20&cursor=<opaque>&sort=size&desc=true&field__eq=value`.
  - `cursor` is an opaque, encrypted keyset cursor from a previous page's
    `next_cursor`; omitted/empty on the first request. It encodes the sort
    key (and id, for stable tie-breaking) of the last row on the previous
    page **plus the `(sort_field, ascending)` it was built for**, encrypted
    via the `EncryptionService` (JWE `dir` + `A256GCM`) so it is tamper-proof.
    The sort field/direction are validated on decode: a cursor reused under a
    different `sort` / `desc` (or no sort) returns `400 invalid_input` rather
    than silently applying the key against the wrong column. A malformed or
    tampered cursor also returns `400 invalid_input`.
  - `limit` is capped (default 20, max 100). There is no `offset`.
  - The response `Page` carries `items`, `limit`, and `next_cursor`
    (`None` when the page is the last). There is no `total` on the page —
    counting is a separate `count` action.
  - Filter operators follow the `field__op=value` convention (`eq`, `ne`,
    `lt`, `lte`, `gt`, `gte`, `contains`, `in`).
* `count` returns the matching row count for a filter as a bare integer
  (e.g. `42`), decoupled from paging and sort. It accepts the **same filter
  query params** as `search` but no `sort` / `limit` / `cursor` (ordering and
  paging are meaningless for a count). Permission reuses `search` — counting
  is not a separate privilege from listing.
* `update` is a partial merge (PATCH semantics), never a full replace.
* `batch_edit` and `batch_read` accept an array and return an array
  positionally aligned with the input: each position `i` holds the entity for
  the `i`-th input id, or `null` if that id does not exist. The response
  length always equals the input length.
* All actions are permission-checked before execution (see `auth-rbac` skill).

## Error shapes

Errors use a consistent envelope:

```json
{"error": {"code": "not_found", "message": "Resource ... not found"}}
```

* `400` — validation error (`invalid_input`)
* `401` — unauthenticated (`unauthenticated`)
* `403` — permission denied (`forbidden`)
* `404` — resource not found (`not_found`)
* `409` — conflict / uniqueness violation (`conflict`)
* `422` — pydantic validation failure (FastAPI default)
* `500` — unexpected server error (`internal_error`)

## Escape hatches

A generated service can be overridden: define a route at the same path and the
framework yields to it. You can also call the repository or session directly
from a custom route when the standard actions are insufficient.
