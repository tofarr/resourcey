---
name: rest-api-routes
description: REST wire conventions — action-to-route mapping, path naming, cursor paging, the filter/sort query surface, batch bodies, and error codes. Load when designing or changing what a client sees.
version: "2.0.0"
---

# REST wire conventions

The implementation lives in the `transport` skill; this is the contract a client
sees.

## Actions

| Action | Method | Path | Body | Status |
|---|---|---|---|---|
| `create` | POST | `/{resource}` | `create_request` | 201 + `create_response` |
| `read` | GET | `/{resource}/{id}` | — | 200 + `read_response` |
| `update` | PATCH | `/{resource}/{id}` | `update_request` (partial) | 200 + `update_response` |
| `delete` | DELETE | `/{resource}/{id}` | — | 204, no body |
| `search` | GET | `/{resource}` | query params | 200 + page |
| `count` | GET | `/{resource}/count` | filter query params | 200 + bare integer |
| `batch_read` | GET | `/{resource}/batch-read` | repeated `?id=` | 200 + list |
| `batch_edit` | POST | `/{resource}/batch-edit` | edit union | 200 + list |

A route exists **only** for a declared action. A write against a read-only
resource is a `405`, not a handler that raises.

## Path naming

* `{resource}` is plural, lower-case, kebab-case, from
  `get_resource_path()` (default: `pluralize(camel_to_kebab(DTO name))`).
* Action sub-paths are dash-separated (`batch-read`, `batch-edit`, `count`).
  Python method names stay snake_case (`batch_read`) — only the URL uses dashes.
* `{id}` is the DTO's identifier (`id` unless `id_field_name` says otherwise).

## Search

```
GET /{resource}?limit=20&cursor=<opaque>&sort=size&desc=true&size__gt=10&name__contains=foo
```

* **Cursor (keyset) pagination, no `offset`.** `cursor` is an opaque, encrypted
  keyset cursor from the previous page's `next_cursor`; omitted on the first
  request. It encodes the sort key and identifier of the last row on the
  previous page **plus the `(sort_field, ascending)` it was built for**,
  encrypted (JWE) so it is tamper-proof.
* A cursor reused under a different `sort` / `desc` (or no sort) is
  `400 invalid_input` rather than silently applying the key against the wrong
  column. A malformed or tampered cursor is also 400.
* `limit` defaults to `20` (`DEFAULT_LIMIT`).
* `sort` names a **sortable** field (derived from the read model); `desc=true`
  flips it. With no `sort`, the identifier order is used and `desc` is ignored.
* An unknown or non-sortable `sort` is `400`.
* The page body is `{"items": [...], "limit": 20, "next_cursor": "..." | null}`.
  There is **no `total`** — counting is the separate `count` action.

### Filter operators

`?<attribute>__<op>=<value>`. The set available on a field is derived from its
type:

| Base type | Operators |
|---|---|
| `bool` | `eq`, `in` |
| `int` / `float` / `Decimal` / `datetime` / `date` / `time` | `eq`, `in`, `gt`, `ge`, `lt`, `le` |
| `str` / `UUID` | the above plus `contains` |
| `dict` / `list` / `set` / Pydantic model | none (not scalarly comparable) |

Multiple filter params are AND-ed. `in` takes a comma-separated list (capped at
500 values). A field is filterable exactly when the read model exposes it, so a
projected-away field is not filterable — and an unknown `field__op` key is
`400 invalid_input`, never silently ignored.

There is no `ne` operator; express inequality as the complement you actually
need, or filter client-side.

## Count

Returns the matching row count as a bare integer, decoupled from paging and
sort. It accepts the **same filter params** as `search` but no
`sort` / `limit` / `cursor` (meaningless for a count). It reuses the `search`
permission — counting is not a separate privilege from listing.

## Batch endpoints

Both return a list **positionally aligned** with the input: position `i` carries
the result for input `i`, or `null` when there is nothing to return. The
response length always equals the input length.

* `batch_read` is a `GET` (a pure read), so ids are repeated query params:
  `?id=a&id=b`. A miss is `null`.
* `batch_edit`'s body is a **`kind`-discriminated union**, so one batch can mix
  operations:

```json
[
  {"kind": "Create", "item": {"label": "a"}},
  {"kind": "Update", "item": {"id": 1, "label": "b"}, "condition": {"kind": "AttrFilter", ...}},
  {"kind": "Delete", "id": 2}
]
```

A `Create` yields the entity projected onto the create response (so a
one-time-reveal field survives); an `Update` onto the update response; a
`Delete`, an absent id, or a failed `condition` yields `null`. The union is
narrowed to the actions the resource declares, so a batch cannot reach an action
the resource never exposed.

## Errors

```json
{"error": {"code": "not_found", "message": "..."}}
```

| Status | `code` |
|---|---|
| 400 | `invalid_input` |
| 401 | *(FastAPI default: `{"detail": ...}` + `WWW-Authenticate`)* |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 422 | *(FastAPI default: pydantic request validation)* |
| 500 | `internal_error` |
| 501 | `unsupported_filter` |

401 and 422 are FastAPI's own shapes — assert on the status (and the
`WWW-Authenticate` header for 401), not on `error.code`.

## Semantics worth remembering

* `update` is a partial merge (PATCH), never a full replace. An omitted field
  with no update default is left unchanged.
* A row outside the caller's policy scope is a `404` on read / update / delete
  (existence is not leaked) and simply absent from search results.
* A denied `create` is a `403`; a denied `search` / `count` is an empty page /
  `0`, never an error.
* `ETag` / `Last-Modified` / `Cache-Control` are emitted when the resource's
  cache strategy provides them; a conditional `GET` / `HEAD` short-circuits to
  `304`.
