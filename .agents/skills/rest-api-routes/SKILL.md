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
| `batch_read` | POST | `/{resource}/batch_read` | list of ids | list of entities |
| `batch_edit` | POST | `/{resource}/batch_edit` | list of edits | list of entities |

## Conventions

* Resource names are plural, lowercase, snake_case in URLs.
* `search` uses query parameters for filtering, sorting, and pagination:
  `?limit=20&offset=0&sort=-created_at&field__eq=value`. Filter operators
  follow the `field__op=value` convention (`eq`, `ne`, `lt`, `lte`, `gt`,
  `gte`, `contains`, `in`).
* `update` is a partial merge (PATCH semantics), never a full replace.
* `batch_edit` and `batch_read` accept an array and return an array in the
  same order as the input ids.
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
