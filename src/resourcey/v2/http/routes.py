"""FastAPI route mounting for a ``v2`` resource's standard actions.

This is the ``v2`` transport layer's route half: it turns a
:class:`~resourcey.v2.core.resource.Resource` declaration into FastAPI routes
for the actions the resource supports, projecting each action's DTO result onto
the matching derived REST model.

:func:`register_routes` resolves the exposed resource once
(``resource.get_exposed_resource()``): a hidden resource registers no routes,
and the exposed resource drives the path, the derived models, the supported
actions, and the service dependency. The per-request service dependency is
built through the configured :class:`~resourcey.v2.http.dependency_builder.DependencyBuilder`
(issue #86): the builder is resolved on the **exposed** resource, so a
projection's wrapped service is the projection's, not the hidden inner
resource's.

Where the ``v2`` seams differ from ``v1``:

* ``v1``'s ``get_create_model`` / ``get_update_model`` / ``get_read_model`` are
  replaced by ``get_rest_models()`` -> :class:`~resourcey.v2.core.dto.RestModels`,
  so each action is mapped to the right shape explicitly (``create`` ->
  ``create_response``, so the one-time-reveal field survives).
* services return DTO instances, so every response is **projected** onto the
  REST model here (:func:`_project`), dropping the ``MISSING`` sentinel.
* filtering is back (issue #79): search and count accept ``<field>__<op>``
  query params, validated against the exposed resource's filter surface (a
  declared :meth:`~resourcey.v2.core.resource.Resource.get_search_filter_type`,
  else derived from the read model). Sorting is back too (issue #97): search
  accepts ``sort`` / ``desc``, validated against the exposed resource's
  :meth:`~resourcey.v2.core.resource.Resource.get_sortable_fields`.
* caching is back (issue #92): the exposed resource's
  :meth:`~resourcey.v2.core.resource.Resource.get_cache_strategy` drives
  ``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires`` headers, and a
  conditional ``GET`` short-circuits to ``304 Not Modified``.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Annotated, Any, Literal, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model
from sqlalchemy.exc import IntegrityError

from resourcey.v2.cache.cache_header import CacheHeader
from resourcey.v2.core.dto import RestModels, request_to_dto
from resourcey.v2.core.errors import InvalidInputError, UnsupportedFilterError
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import (
    Action,
    Create,
    Delete,
    NotFoundError,
    Service,
    ServiceError,
    Update,
)
from resourcey.v2.http.dependency_builder import DefaultDependencyBuilder, DependencyBuilder
from resourcey.v2.util.missing import MISSING
from resourcey.v2.util.search_filter import SEPARATOR, SearchFilter, build_filter

T = TypeVar("T", bound=BaseModel)


def register_routes(
    app_or_router: FastAPI | APIRouter,
    resource: Resource[Any],
    *,
    prefix: str = "",
    tags: Sequence[str] | None = None,
    dependency_builder: DependencyBuilder | None = None,
) -> APIRouter:
    """Build an :class:`APIRouter` with the exposed resource's action routes and include it.

    Accepts a ``FastAPI`` app, an ``APIRouter``, or any object with
    ``include_router`` (duck-typed). Resolution order:

    1. ``exposed = resource.get_exposed_resource()``. When it is ``None`` the
       resource is internal-only and **no** routes are registered — the sole
       gate on exposure.
    2. ``exposed`` drives the path, the derived models, the supported actions,
       and the service dependency.

    ``dependency_builder`` decides how the per-request service dependency is
    built (issue #86); it defaults to
    :class:`~resourcey.v2.http.dependency_builder.DefaultDependencyBuilder`. It
    is resolved on ``exposed``, so a projection's wrapped service is the
    projection's.

    The ``{resource}`` path segment is the plural, lower-case, kebab-case name
    from ``exposed.get_resource_path()``; sub-paths use dashes (``batch-read``,
    ``batch-edit``, ``count``). ``tags`` defaults to ``[<RESOURCE_NAME>]`` (the
    exposed resource's class name). A route is only added if none already
    exists at that path + method on the target router — a developer who
    registers a custom route first keeps it (escape hatch). Returns the built
    :class:`APIRouter`.
    """
    exposed = resource.get_exposed_resource()
    if exposed is None:
        # Hidden resource: no routes. The (empty) router's tag is irrelevant.
        return APIRouter(tags=list(tags) if tags else [type(resource).__name__])

    builder = dependency_builder if dependency_builder is not None else DefaultDependencyBuilder()
    router = APIRouter(tags=list(tags) if tags else [type(exposed).__name__])
    path = "/" + exposed.get_resource_path().lstrip("/")
    models = exposed.get_rest_models()
    dto_model = exposed.get_dto_type()
    id_field = exposed.get_id_field()
    id_type = _id_python_type(models, id_field)
    service_dep = _service_dependency(exposed, builder)
    supported = exposed.get_supported_actions()
    strategy = exposed.get_cache_strategy()

    # Static sub-paths (search / count / batch-read / batch-edit) are registered
    # before the ``{id}`` routes, otherwise ``batch-read`` would be captured as
    # an id value by the ``/{resource}/{id}`` route.
    if Action.SEARCH in supported:
        _add_search_route(router, path, models, exposed, service_dep, strategy)
    if Action.COUNT in supported:
        _add_count_route(router, path, exposed, service_dep, strategy)
    if Action.BATCH_READ in supported:
        _add_batch_read_route(router, path, models, id_type, service_dep, strategy)
    if Action.BATCH_EDIT in supported:
        _add_batch_edit_route(
            router, path, models, dto_model, id_field, id_type, service_dep, strategy
        )
    if Action.CREATE in supported:
        _add_create_route(router, path, models, dto_model, service_dep, strategy)
    if Action.READ in supported:
        _add_read_route(router, path, models, id_type, service_dep, strategy)
    if Action.UPDATE in supported:
        _add_update_route(router, path, models, dto_model, id_field, id_type, service_dep, strategy)
    if Action.DELETE in supported:
        _add_delete_route(router, path, id_type, service_dep)

    app_or_router.include_router(router, prefix=_normalize_prefix(prefix))
    return router


# ---------------------------------------------------------------------------
# Service dependency (get_service -> FastAPI dependency)
# ---------------------------------------------------------------------------


def _service_dependency(
    resource: Resource[T], builder: DependencyBuilder
) -> Callable[..., AsyncIterator[Service[T, Any]]]:
    """Resolve the per-request service dependency for ``resource`` via ``builder``.

    The builder is consulted **once, here, at registration time** (issue #86);
    the callable it returns is what FastAPI invokes per request. It is
    generic over the DTO type, so a route's injected ``service`` is typed
    against the resource's DTO.
    """
    dependency = builder.get_service_dependency(resource)
    if not callable(dependency):
        raise TypeError(
            f"{type(builder).__name__}.get_service_dependency() returned a "
            f"non-callable {dependency!r}; a builder must return a FastAPI dependency."
        )
    return cast("Callable[..., AsyncIterator[Service[T, Any]]]", dependency)


# ---------------------------------------------------------------------------
# Filter surface (query params -> filter instance)
# ---------------------------------------------------------------------------


def _filter_surface(resource: Resource[Any]) -> dict[str, tuple[Any, frozenset[str]]]:
    """The exposed resource's filter surface: attribute -> (annotation, ops).

    Two sources, matching the two candidates in issue #79:

    * a **declared** filter class (:meth:`get_search_filter_type`) — its
      ``<attribute>__<op>`` fields are the whole surface (v1's opt-in style);
    * the **derived** surface (:meth:`get_filter_operators`) — a field is
      filterable exactly when the read model exposes it, with the operator set
      fixed by the field's type.

    The returned mapping drives both the generated FastAPI query parameters and
    the rejection of unknown ``field__op`` params.
    """
    declared = resource.get_search_filter_type()
    if declared is not None:
        fields = declared.model_fields
        surface: dict[str, tuple[Any, frozenset[str]]] = {}
        for name, field in fields.items():
            attribute, sep, op = name.rpartition(SEPARATOR)
            if not sep or attribute == "":
                continue
            existing = surface.get(attribute)
            ops = (existing[1] if existing else frozenset()) | {op}
            surface[attribute] = (field.annotation, ops)
        return surface

    read_fields = resource.get_rest_models().read_response.model_fields
    return {
        attribute: (read_fields[attribute].annotation, ops)
        for attribute, ops in resource.get_filter_operators().items()
        if attribute in read_fields
    }


def _filter_dependency(
    surface: Mapping[str, tuple[Any, frozenset[str]]],
) -> Callable[..., dict[str, Any]]:
    """Build a FastAPI dependency exposing each ``<attribute>__<op>`` as a query param.

    The signature is synthesised (one typed, ``Query``-defaulted parameter per
    attribute/operator pair) so FastAPI unfolds them into OpenAPI parameters and
    coerces each value before the handler runs. Unknown params are not rejected
    by FastAPI, so the handler still validates the raw query keys.
    """
    parameters: list[inspect.Parameter] = []
    for attribute, (annotation, ops) in surface.items():
        for op in sorted(ops):
            # ``contains`` is a substring test, so its value is always a string;
            # the ordering / equality operators keep the field's own type.
            value_annotation = str if op == "contains" else annotation
            parameters.append(
                inspect.Parameter(
                    f"{attribute}{SEPARATOR}{op}",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=Query(default=None),
                    annotation=value_annotation,
                )
            )

    def dependency(**kwargs: Any) -> dict[str, Any]:
        return {name: value for name, value in kwargs.items() if value is not None}

    dependency.__signature__ = inspect.Signature(parameters=parameters)  # type: ignore[attr-defined]
    return dependency


def _resolve_filters(
    request: Request,
    surface: Mapping[str, tuple[Any, frozenset[str]]],
    values: Mapping[str, Any],
) -> SearchFilter[Any] | None:
    """Validate the ``field__op`` query keys and build a standard filter tree.

    FastAPI collects the declared params into ``values`` (typed / coerced) and
    surfaces them in OpenAPI, but it silently ignores unknown query parameters.
    To keep a typo from being dropped, any ``field__op`` key outside the surface
    is rejected with :class:`InvalidInputError` (mapped to ``400``). With no
    surface at all, every filter param is rejected. Returns ``None`` when no
    filter params were supplied.
    """
    present = {key for key in request.query_params if SEPARATOR in key}
    if not present:
        return None
    known = {
        f"{attribute}{SEPARATOR}{op}" for attribute, (_ann, ops) in surface.items() for op in ops
    }
    if not known:
        raise InvalidInputError(
            f"Filter parameters {sorted(present)} are not supported on this resource"
        )
    unknown = present - known
    if unknown:
        raise InvalidInputError(f"Unknown filter parameters {sorted(unknown)}")
    clauses = []
    for name, value in values.items():
        attribute, sep, op = name.rpartition(SEPARATOR)
        if sep and attribute:
            clauses.append((attribute, op, value))
    if not clauses:
        return None
    return build_filter(clauses)


# ---------------------------------------------------------------------------
# Route builders
# ---------------------------------------------------------------------------


def _add_create_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    service_dep: Any,
    strategy: Any,
) -> None:
    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        created = await service.create(request_to_dto(dto_model, payload))
        projected = _project(created, models.create_response)
        header = _header_for(strategy, [projected])
        return _cached_json_response(request, _dump(projected), header, status.HTTP_201_CREATED)

    handler.__annotations__ = {
        "request": Request,
        "payload": models.create_request,
        "service": Service,
    }
    _route(router, path, ["POST"], handler, status_code=status.HTTP_201_CREATED)


def _add_read_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
) -> None:
    async def handler(request, id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.read(id)
        projected = _project(found, models.read_response)
        header = _header_for(strategy, [projected])
        return _cached_json_response(request, _dump(projected), header)

    handler.__annotations__ = {"request": Request, "id": id_type, "service": Service}
    _route(router, f"{path}/{{id}}", ["GET"], handler)


def _add_update_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    id_field: str,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
) -> None:
    async def handler(request, id, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        updated = await service.update(_update_dto(dto_model, id_field, id, payload))
        projected = _project(updated, models.update_response)
        header = _header_for(strategy, [projected])
        return _cached_json_response(request, _dump(projected), header)

    handler.__annotations__ = {
        "request": Request,
        "id": id_type,
        "payload": models.update_request,
        "service": Service,
    }
    _route(router, f"{path}/{{id}}", ["PATCH"], handler)


def _add_delete_route(router: APIRouter, path: str, id_type: Any, service_dep: Any) -> None:
    async def handler(id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        await service.delete(id)
        return None

    handler.__annotations__ = {"id": id_type, "service": Service}
    _route(router, f"{path}/{{id}}", ["DELETE"], handler, status_code=status.HTTP_204_NO_CONTENT)


def _add_search_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    exposed: Resource[Any],
    service_dep: Any,
    strategy: Any,
) -> None:
    """Register ``GET /{resource}`` — cursor-paginated, filterable, sortable search.

    ``limit`` and ``cursor`` paginate; ``sort`` / ``desc`` order the page
    (resolved by the exposed resource's
    :meth:`~resourcey.v2.core.resource.Resource.resolve_sort_order`, an unknown
    or non-sortable field -> ``400``); declared ``<field>__<op>`` query params
    are collected into a standard :class:`SearchFilter` and pushed down. The
    filter surface (fields + operators) comes from the exposed resource's
    :meth:`~resourcey.v2.core.resource.Resource.get_filter_operators` (or a
    declared :meth:`~...get_search_filter_type`); an unknown field or operator,
    or any filter on a resource with no surface, is rejected ``400``.

    The resolved ordering and filter are passed to ``search`` as plain
    arguments (``search_filter`` / ``sort_order`` / ``cursor`` / ``limit``) rather
    than wrapped in a request object.
    """
    filter_spec = _filter_surface(exposed)
    filter_dep = _filter_dependency(filter_spec)

    async def handler(  # type: ignore[no-untyped-def]
        request,
        limit=20,
        cursor=None,
        sort=None,
        desc=False,
        values=Depends(filter_dep),  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        filters = _resolve_filters(request, filter_spec, values)
        page = await service.search(
            search_filter=filters,
            sort_order=exposed.resolve_sort_order(sort, desc),
            cursor=cursor,
            limit=limit,
        )
        body, items = _page_body(page, models.search_response)
        header = _header_for(strategy, items)
        return _cached_json_response(request, body, header)

    handler.__annotations__ = {
        "request": Request,
        "limit": int,
        "cursor": str | None,
        "sort": str | None,
        "desc": bool,
        "values": dict,
        "service": Service,
    }
    _route(router, path, ["GET"], handler)


def _add_count_route(
    router: APIRouter,
    path: str,
    exposed: Resource[Any],
    service_dep: Any,
    strategy: Any,
) -> None:
    """Register ``GET /{resource}/count`` — the count of matching rows.

    Accepts the same ``<field>__<op>`` filter surface as search, and contributes
    the resolved filter to the cache ETag so distinct filters get distinct
    validators.
    """
    filter_spec = _filter_surface(exposed)
    filter_dep = _filter_dependency(filter_spec)

    async def handler(  # type: ignore[no-untyped-def]
        request,
        values=Depends(filter_dep),  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        filters = _resolve_filters(request, filter_spec, values)
        total = await service.count(search_filter=filters)
        header: CacheHeader | None = None
        if strategy is not None:
            candidate = strategy.count_cache_header(total, filters)
            header = candidate if candidate.has_any() else None
        return _cached_json_response(request, total, header)

    handler.__annotations__ = {"request": Request, "values": dict, "service": Service}
    _route(router, f"{path}/count", ["GET"], handler)


def _add_batch_read_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
) -> None:
    async def handler(request, id=Query(default=[]), service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.batch_read(list(id))
        items = [_project(item, models.search_response) for item in found]
        header = _header_for(strategy, items)
        return _cached_json_response(request, _dump(items), header)

    handler.__annotations__ = {"request": Request, "id": list[id_type], "service": Service}
    _route(router, f"{path}/batch-read", ["GET"], handler)


def _add_batch_edit_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    id_field: str,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
) -> None:
    """Register ``POST /{resource}/batch-edit`` — mixed create / update / delete.

    The body is a list discriminated by ``kind`` (``Create`` / ``Update`` /
    ``Delete``), so one batch can create, update, *and* delete. Each item is
    folded into the matching :class:`~resourcey.v2.core.service.Edit` node and
    the results align positionally with the input (a delete, or a miss, is
    ``None``).
    """
    body_model = _batch_edit_body(models.create_request, models.update_request, id_field, id_type)

    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        edits = [_batch_edit_node(item, dto_model, id_field) for item in payload]
        edited = await service.batch_edit(edits)
        items = [
            _project_edit_result(edit, result, models)
            for edit, result in zip(edits, edited, strict=True)
        ]
        header = _header_for(strategy, items)
        return _cached_json_response(request, _dump(items), header)

    handler.__annotations__ = {
        "request": Request,
        "payload": list[body_model],  # type: ignore[valid-type]
        "service": Service,
    }
    _route(router, f"{path}/batch-edit", ["POST"], handler)


# ---------------------------------------------------------------------------
# Route escape hatch + projection helpers
# ---------------------------------------------------------------------------


def _route(
    router: APIRouter,
    path: str,
    methods: list[str],
    handler: Callable[..., Any],
    **kwargs: Any,
) -> None:
    """Register a route unless one already exists at path+method (escape hatch)."""
    existing: set[tuple[str, str]] = set()
    for route in router.routes:
        route_methods = getattr(route, "methods", None) or set()
        for method in route_methods:
            existing.add((getattr(route, "path", ""), method))
    if any((path, method) in existing for method in methods):
        return
    router.add_api_route(path, handler, methods=methods, response_model=None, **kwargs)


def _project(instance: Any, model: type[BaseModel]) -> BaseModel | None:
    """Project a DTO instance onto a derived REST model (dropping ``MISSING``).

    The DTO is the internal type; the wire body is the projection. Fields the
    service left ``MISSING`` are omitted so the REST model's own defaults apply.
    ``None`` (a ``batch_read`` / ``batch_edit`` miss) projects to ``None``.

    Returns the validated model instance (not a dict) so the cache strategy can
    hash exactly the representation that will be serialised.
    """
    if instance is None:
        return None
    values = {name: value for name, value in vars(instance).items() if value is not MISSING}
    return model.model_validate(values)


def _dump(projected: Any) -> Any:
    """Serialise a projected model / list / scalar to a JSON-ready value."""
    if isinstance(projected, BaseModel):
        return projected.model_dump(mode="json")
    if isinstance(projected, list):
        return [_dump(item) for item in projected]
    return projected


def _page_body(page: Any, search_response: type[BaseModel]) -> tuple[dict[str, Any], list[Any]]:
    """Serialise a :class:`~resourcey.v2.core.service.Page`; return body + projected items.

    The projected items are returned alongside the body so the cache strategy
    hashes the same representations the response carries.
    """
    items = [_project(item, search_response) for item in page.items]
    return {
        "items": [_dump(item) for item in items],
        "limit": page.limit,
        "next_cursor": page.next_cursor,
    }, items


# ---------------------------------------------------------------------------
# HTTP caching (ETag / Last-Modified / Cache-Control + 304 short-circuit)
# ---------------------------------------------------------------------------


def _header_for(strategy: Any, items: list[Any]) -> CacheHeader | None:
    """Compute the cache header for ``items`` via the resource's strategy.

    ``None`` when the resource declares no strategy or the strategy yields
    nothing (no validators and no freshness), so the response is uncached.
    """
    if strategy is None:
        return None
    header = strategy.get_cache_header(items)
    return header if header.has_any() else None


def _http_date(value: datetime) -> str:
    """Format a datetime as an RFC 7231 IMF-fixdate (GMT)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return format_datetime(value.astimezone(UTC), usegmt=True)


def _cache_response_headers(header: CacheHeader) -> dict[str, str]:
    """Build the ``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires``
    response headers from a :class:`CacheHeader`'s non-``None`` fields."""
    has_validator = header.etag is not None or header.updated_at is not None
    headers: dict[str, str] = {}
    if header.etag is not None:
        headers["ETag"] = header.etag
    if header.updated_at is not None:
        headers["Last-Modified"] = _http_date(header.updated_at)
    # ``private`` keeps a caller-scoped freshness window out of shared caches;
    # it is orthogonal to the validators, so it is a directive on whatever
    # Cache-Control the freshness/validator rules produce.
    directives: list[str] = ["private"] if header.private else []
    if header.expire_at is not None:
        # max-age is the remaining freshness window (the strategy's expire_in,
        # computed moments ago). Rounding preserves the integer seconds clients
        # expect in Cache-Control.
        now = datetime.now(UTC)
        max_age = max(0, int((header.expire_at - now).total_seconds()))
        directives.append(f"max-age={max_age}")
        headers["Cache-Control"] = ", ".join(directives)
        headers["Expires"] = _http_date(header.expire_at)
    elif has_validator or directives:
        # Validators with no freshness window: force revalidation on every use.
        # Without a Cache-Control directive a browser falls back to heuristic
        # freshness and serves from cache without ever echoing the validator
        # back, so the conditional-request path (and 304s) never fires.
        if has_validator:
            directives.append("no-cache")
        headers["Cache-Control"] = ", ".join(directives)
    return headers


def _client_cache_header(request: Request) -> CacheHeader:
    """Map a request's ``If-None-Match`` / ``If-Modified-Since`` into a
    :class:`CacheHeader` (the client's validators). ``expire_at`` is not a
    client validator, so it is always ``None`` here."""
    etag = request.headers.get("if-none-match")
    if_modified_since = request.headers.get("if-modified-since")
    updated_at: datetime | None = None
    if if_modified_since:
        try:
            parsed = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            updated_at = parsed
    return CacheHeader(etag=etag, updated_at=updated_at)


def _cached_json_response(
    request: Request,
    body: Any,
    header: CacheHeader | None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Serialise ``body`` as JSON, applying cache headers and conditional
    ``304`` short-circuiting.

    When ``header`` is ``None`` (the strategy yielded nothing) this is a plain
    JSON response. Otherwise the validator + freshness headers are set, and if
    the request is a safe method (``GET`` / ``HEAD`` - RFC 7232 restricts
    ``304 Not Modified`` to safe methods) and the client's conditional request
    headers prove the copy is current (``header.is_modified(client)`` is
    ``False``) a ``304 Not Modified`` with an empty body (but the validator +
    ``Cache-Control`` headers) is returned. Unsafe methods (``POST`` / ``PATCH``
    / ``DELETE``) still emit the headers on the response but always send the
    body - they cannot short-circuit to ``304``.
    """
    if header is None:
        return _json_response(body, status_code)
    if request.method in ("GET", "HEAD") and not header.is_modified(_client_cache_header(request)):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers=_cache_response_headers(header),
        )
    response = _json_response(body, status_code)
    response.headers.update(_cache_response_headers(header))
    return response


def _json_response(body: Any, status_code: int = status.HTTP_200_OK) -> JSONResponse:
    """Serialise an already-dumped body as a JSON response."""
    return JSONResponse(content=jsonable_encoder(body), status_code=status_code)


def _id_python_type(models: RestModels, id_field: str) -> Any:
    """The Python type of the identifier, read off the derived read model."""
    annotation = models.read_response.model_fields[id_field].annotation
    return annotation if isinstance(annotation, type) else str


def _batch_edit_body(
    create_request: type[BaseModel],
    update_request: type[BaseModel],
    id_field: str,
    id_type: Any,
) -> Any:
    """Build the ``batch-edit`` body: a ``kind``-discriminated union of edits.

    Three wire shapes, matching :class:`~resourcey.v2.core.service.Create`,
    :class:`~resourcey.v2.core.service.Update`, and
    :class:`~resourcey.v2.core.service.Delete`:

    * ``{kind: "Create", item: <create_request>}``
    * ``{kind: "Update", item: {<id_field>, ...update_request fields}}``
    * ``{kind: "Delete", <id_field>: <id>}``

    The update item carries the identifier because the path does not; a create
    item is the create request verbatim (a server-generated id is absent, a
    natural key is client-supplied).
    """
    create_edit = create_model(
        f"{create_request.__name__}CreateEdit",
        kind=(Literal["Create"], ...),
        item=(create_request, ...),
    )
    update_fields = {
        name: (field.annotation, field) for name, field in update_request.model_fields.items()
    }
    update_item = create_model(  # type: ignore[call-overload]
        f"{update_request.__name__}BatchUpdateItem",
        **{id_field: (id_type, ...)},  # the id is required on each update
        **update_fields,
    )
    update_edit = create_model(
        f"{update_request.__name__}UpdateEdit",
        kind=(Literal["Update"], ...),
        item=(update_item, ...),
    )
    delete_edit = create_model(  # type: ignore[call-overload]
        f"{create_request.__name__}DeleteEdit",
        kind=(Literal["Delete"], ...),
        **{id_field: (id_type, ...)},
    )
    edits = create_edit | update_edit | delete_edit
    return Annotated[edits, Field(discriminator="kind")]


def _batch_edit_node(item: BaseModel, dto_model: type[BaseModel], id_field: str) -> Any:
    """Fold a validated ``batch-edit`` body item into its :class:`Edit` node."""
    kind = item.kind  # type: ignore[attr-defined]
    if kind == "Create":
        return Create(item=request_to_dto(dto_model, item.item))  # type: ignore[attr-defined]
    if kind == "Update":
        return Update(item=request_to_dto(dto_model, item.item))  # type: ignore[attr-defined]
    return Delete(id=getattr(item, id_field))


def _project_edit_result(
    edit: Any, result: BaseModel | None, models: RestModels
) -> BaseModel | None:
    """Project a batch-edit result onto the shape for its edit.

    A delete yields ``None`` (nothing to return); a create projects onto the
    create response (so a one-time-reveal field survives) and an update onto the
    update response. A miss (``result`` is ``None``) stays ``None``.
    """
    if isinstance(edit, Delete) or result is None:
        return None
    if isinstance(edit, Create):
        return _project(result, models.create_response)
    return _project(result, models.update_response)


def _update_dto(
    dto_model: type[BaseModel], id_field: str, id_value: Any, payload: BaseModel
) -> BaseModel:
    """Build the update DTO from the request body, injecting the path identifier.

    ``request_to_dto`` is the single sanctioned request->DTO hop (``exclude_unset``);
    the id is set here so the DTO carries its own identifier and the service takes
    just the DTO.
    """
    dto = request_to_dto(dto_model, payload)
    setattr(dto, id_field, id_value)
    return dto


def _normalize_prefix(prefix: str) -> str:
    """Normalise a mount prefix: ``"/"`` means "mount at the root" (``""``)."""
    return "" if prefix == "/" else prefix


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def register_error_handlers(app: FastAPI) -> None:
    """Register the consistent error envelope on a FastAPI app.

    Maps the exceptions ``v2`` has today to the documented status + code:

    * ``NotFoundError`` -> 404 ``not_found``
    * ``InvalidInputError`` -> 400 ``invalid_input``
    * ``UnsupportedFilterError`` -> 501 ``unsupported_filter``
    * ``IntegrityError`` -> 409 ``conflict``
    * ``ServiceError`` -> 500 ``internal_error``
    * Pydantic validation failures keep FastAPI's 422 (its default handler).

    The wider ``ResourceyError`` hierarchy (issue #83) extends this same
    function.
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response("not_found", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(InvalidInputError)
    async def _invalid_input(_: Request, exc: InvalidInputError) -> JSONResponse:
        return _error_response("invalid_input", str(exc), status.HTTP_400_BAD_REQUEST)

    @app.exception_handler(UnsupportedFilterError)
    async def _unsupported_filter(_: Request, exc: UnsupportedFilterError) -> JSONResponse:
        return _error_response("unsupported_filter", str(exc), status.HTTP_501_NOT_IMPLEMENTED)

    @app.exception_handler(IntegrityError)
    async def _conflict(_: Request, exc: IntegrityError) -> JSONResponse:
        return _error_response("conflict", str(exc.orig), status.HTTP_409_CONFLICT)

    @app.exception_handler(ServiceError)
    async def _internal(_: Request, exc: ServiceError) -> JSONResponse:
        return _error_response("internal_error", str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": code, "message": message}},
        status_code=status_code,
    )
