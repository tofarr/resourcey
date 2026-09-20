"""FastAPI route mounting for a resource's standard actions.

The route builder asks the resource for a service via
:meth:`~resourcey.resource.sql.SqlResource.open_service` (an async context
manager that opens/reuses a session and yields a
:class:`~resourcey.resource.service.SqlService`), reads
:meth:`~resourcey.resource.base.BaseResource.get_supported_actions`, and
wires only those routes. Each handler receives the service as a FastAPI
dependency and calls its action methods directly (no session parameter - the
session is instance state on the service).

This module is the HTTP concern: response serialisation, cache headers,
conditional ``304`` short-circuiting, error-envelope handlers, and filter /
sort query-param wiring. It is deliberately separate from the service so the
service stays HTTP-free and wrappable (issue #40).
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request as StarletteRequest

from resourcey.cache.cache_header import CacheHeader
from resourcey.resource.errors import InvalidInputError, NotFoundError
from resourcey.resource.missing import MISSING
from resourcey.resource.service import Page, SqlService
from resourcey.resource.service_base import ServiceError

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


# Default pagination bounds (kept in sync with the service's defaults so the
# OpenAPI schema documents the same default ``limit`` the service applies).
_DEFAULT_LIMIT = 20


def register_routes(
    app_or_router: FastAPI | APIRouter,
    resource: type[BaseResource],
    *,
    prefix: str = "",
    tags: list[str] | None = None,
) -> APIRouter:
    """Build an :class:`APIRouter` with the resource's supported-action routes and include it.

    Accepts a ``FastAPI`` app, an ``APIRouter``, or any object with
    ``include_router`` (duck-typed). The ``{resource}`` path segment is the
    plural, lower-case, kebab-case name from ``resource.get_resource_path()``;
    action sub-paths use dashes (``batch-read``, ``batch-edit``, ``count``).
    ``tags`` defaults to ``[<RESOURCE_NAME>]`` (the resource class name).

    Routes are wired only for ``resource.get_supported_actions()``. Each
    handler resolves the service via ``resource.open_service`` (a FastAPI
    dependency that opens a session, yields the service, and commits/closes
    on exit) and calls the matching action method (no session parameter). A
    route is only added if no route already exists at that path + method on
    the target router - a developer who registers a custom route first keeps
    it (escape hatch). Returns the built :class:`APIRouter`.
    """
    router = APIRouter(tags=tags or [resource.__name__])  # type: ignore[arg-type]
    path = "/" + resource.get_resource_path().lstrip("/")
    id_type = _id_python_type(resource)
    service_dep = _service_dependency(resource)
    supported = resource.get_supported_actions()

    # Build a route per supported action. Static sub-paths (search / count /
    # batch-read / batch-edit) are registered before the ``{id}`` routes,
    # otherwise ``batch-read`` would be captured as an id value by the
    # ``/{resource}/{id}`` route.
    from resourcey.resource.service_base import Action

    if Action.SEARCH in supported:
        _add_search_route(router, path, resource, service_dep)
    if Action.COUNT in supported:
        _add_count_route(router, path, resource, service_dep)
    if Action.BATCH_READ in supported:
        _add_batch_read_route(router, path, id_type, resource, service_dep)
    if Action.BATCH_EDIT in supported:
        _add_batch_edit_route(router, path, resource, service_dep)
    if Action.CREATE in supported:
        _add_create_route(router, path, resource, service_dep)
    if Action.READ in supported:
        _add_read_route(router, path, id_type, resource, service_dep)
    if Action.UPDATE in supported:
        _add_update_route(router, path, id_type, resource, service_dep)
    if Action.DELETE in supported:
        _add_delete_route(router, path, id_type, resource, service_dep)

    app_or_router.include_router(router, prefix=prefix)
    return router


# ---------------------------------------------------------------------------
# Service dependency (open_service -> FastAPI dependency)
# ---------------------------------------------------------------------------


def _service_dependency(resource: type[BaseResource]) -> Callable[..., Any]:
    """Build a FastAPI dependency that yields a service via ``open_service``.

    ``open_service`` is an async context manager that opens (or reuses) a
    session on ``request.state`` and yields the service. FastAPI's dependency
    machinery drives the ``async with``; the session is committed/closed on
    exit. Multiple resources in one request share the same session via
    ``request.state``.
    """

    async def dependency(request: Request) -> Any:
        async with resource.open_service(request) as service:
            yield service

    return dependency


# ---------------------------------------------------------------------------
# Route builders
# ---------------------------------------------------------------------------


def _add_create_route(
    router: APIRouter, path: str, resource: type[BaseResource], service_dep: Any
) -> None:
    create_model = resource.get_create_model()

    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        result = await service.create(payload)
        header = service.compute_cache_header([result])
        return _cached_json_response(
            request, result, service._ctx(), header, status.HTTP_201_CREATED
        )

    handler.__annotations__ = {"request": Request, "payload": create_model, "service": SqlService}
    _route(router, path, ["POST"], handler, response_model=None)


def _add_read_route(
    router: APIRouter, path: str, id_type: Any, resource: type[BaseResource], service_dep: Any
) -> None:
    async def handler(request, id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        result = await service.read(id)
        header = service.compute_cache_header([result])
        return _cached_json_response(request, result, service._ctx(), header)

    handler.__annotations__ = {"request": Request, "id": id_type, "service": SqlService}
    _route(router, f"{path}/{{id}}", ["GET"], handler, response_model=None)


def _add_update_route(
    router: APIRouter, path: str, id_type: Any, resource: type[BaseResource], service_dep: Any
) -> None:
    update_model = resource.get_update_model()

    async def handler(request, id, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        result = await service.update(id, payload)
        header = service.compute_cache_header([result])
        return _cached_json_response(request, result, service._ctx(), header)

    handler.__annotations__ = {
        "request": Request,
        "id": id_type,
        "payload": update_model,
        "service": SqlService,
    }
    _route(router, f"{path}/{{id}}", ["PATCH"], handler, response_model=None)


def _add_delete_route(
    router: APIRouter, path: str, id_type: Any, resource: type[BaseResource], service_dep: Any
) -> None:
    async def handler(id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        await service.delete(id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    handler.__annotations__ = {"id": id_type, "service": SqlService}
    _route(router, f"{path}/{{id}}", ["DELETE"], handler, response_model=None)


def _add_search_route(
    router: APIRouter, path: str, resource: type[BaseResource], service_dep: Any
) -> None:
    filter_cls = resource.get_search_filter_type()
    filter_dep = _filter_dependency(filter_cls) if filter_cls is not None else None
    sortable = resource.get_sortable_fields()
    read_model = resource.get_read_model()

    if sortable:
        handler = _sortable_search_handler(resource, filter_cls, filter_dep, sortable, service_dep)
    else:
        handler = _sortless_search_handler(resource, filter_cls, filter_dep, service_dep)
    _route(router, path, ["GET"], handler, response_model=Page[read_model])  # type: ignore[valid-type]


def _sortable_search_handler(
    resource: type[BaseResource],
    filter_cls: type[SearchFilter[Any]] | None,
    filter_dep: Callable[..., Any] | None,
    sortable: list[str],
    service_dep: Any,
) -> Callable[..., Any]:
    """Build the GET search handler for a resource with sortable fields.

    ``sort`` is an enum of the resource's sortable fields, so FastAPI
    validates it (422 on an unknown / injected value) before the handler
    runs. ``desc`` selects direction (default ascending). ``cursor`` is an
    opaque keyset cursor from a previous page's ``next_cursor``.
    """
    # A dynamic StrEnum (member names are the field names) gives OpenAPI a
    # concrete enum and FastAPI request validation that rejects unknown /
    # injected sort values.
    sort_enum = enum.StrEnum(  # type: ignore[misc]
        f"{resource.__name__}SortField", {n: n for n in sortable}
    )
    sort_default: Any = Query(default=None)
    desc_default: Any = Query(default=False)
    cursor_default: Any = Query(default=None)

    async def handler(  # type: ignore[no-untyped-def]
        request,
        limit=_DEFAULT_LIMIT,
        cursor=cursor_default,
        sort=sort_default,
        desc=desc_default,
        filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        resolved = _resolve_filters(request, filter_cls, filters, resource)
        page = await service.search(
            limit=limit,
            cursor=cursor,
            sort=sort.value if sort is not None else None,
            desc=desc,
            filters=resolved,
        )
        header = service.compute_cache_header(page.items)
        return _cached_json_response(request, page, service._ctx(), header)

    handler.__annotations__ = {
        "request": Request,
        "limit": int,
        "cursor": str | None,
        "sort": sort_enum | None,
        "desc": bool,
        "service": SqlService,
    }
    return handler


def _sortless_search_handler(
    resource: type[BaseResource],
    filter_cls: type[SearchFilter[Any]] | None,
    filter_dep: Callable[..., Any] | None,
    service_dep: Any,
) -> Callable[..., Any]:
    """Build the GET search handler for a resource with no sortable fields.

    ``sort`` / ``desc`` are not exposed. A residual check preserves the
    contract that ``?sort=`` on a sortless resource is rejected (400) rather
    than silently ignored. ``cursor`` is an opaque keyset cursor from a
    previous page's ``next_cursor``.
    """
    cursor_default: Any = Query(default=None)

    async def handler(  # type: ignore[no-untyped-def]
        request,
        limit=_DEFAULT_LIMIT,
        cursor=cursor_default,
        filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        if "sort" in request.query_params or "desc" in request.query_params:
            raise InvalidInputError(
                f"Sort parameters are not supported on {resource.__name__}; "
                f"it declares no sortable fields."
            )
        resolved = _resolve_filters(request, filter_cls, filters, resource)
        page = await service.search(limit=limit, cursor=cursor, filters=resolved)
        header = service.compute_cache_header(page.items)
        return _cached_json_response(request, page, service._ctx(), header)

    handler.__annotations__ = {
        "request": Request,
        "limit": int,
        "cursor": str | None,
        "service": SqlService,
    }
    return handler


def _add_count_route(
    router: APIRouter, path: str, resource: type[BaseResource], service_dep: Any
) -> None:
    """Register ``GET /{resource}/count`` - matching row count for a filter.

    Accepts the same ``field__op=value`` filter query params as ``search``
    (filter validation is shared via :func:`_resolve_filters`), but no
    ``sort`` / ``limit`` / ``cursor`` (ordering and paging are meaningless for
    a count). Returns a bare integer.
    """
    filter_cls = resource.get_search_filter_type()
    filter_dep = _filter_dependency(filter_cls) if filter_cls is not None else None
    count_path = f"{path}/count"

    async def handler(  # type: ignore[no-untyped-def]
        request,
        filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        if {"sort", "desc", "limit", "cursor"} & set(request.query_params):
            raise InvalidInputError(
                "count accepts only filter parameters; sort/limit/cursor are not allowed."
            )
        resolved = _resolve_filters(request, filter_cls, filters, resource)
        total = await service.count(filters=resolved)
        header = service.compute_count_cache_header(total, resolved)
        return _cached_json_response(request, total, None, header)

    handler.__annotations__ = {"request": Request, "service": SqlService}
    _route(router, count_path, ["GET"], handler, response_model=None)


def _add_batch_read_route(
    router: APIRouter, path: str, id_type: Any, resource: type[BaseResource], service_dep: Any
) -> None:
    batch_path = f"{path}/batch-read"

    async def handler(request, id=Query(default=[]), service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        result = await service.batch_read(list(id))
        header = service.compute_cache_header(result)
        return _cached_json_response(request, result, service._ctx(), header)

    handler.__annotations__ = {"request": Request, "id": list[id_type], "service": SqlService}
    _route(router, batch_path, ["GET"], handler, response_model=None)


def _add_batch_edit_route(
    router: APIRouter, path: str, resource: type[BaseResource], service_dep: Any
) -> None:
    batch_path = f"{path}/batch-edit"
    item_model = _batch_edit_item_model(resource)

    async def handler(request, edits, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        tuples = [
            (getattr(item, resource.get_id_field()), _item_to_update_model(item, resource))
            for item in edits
        ]
        result = await service.batch_edit(tuples)
        header = service.compute_cache_header(result)
        return _cached_json_response(request, result, service._ctx(), header)

    handler.__annotations__ = {
        "request": Request,
        "edits": list[item_model],  # type: ignore[valid-type]
        "service": SqlService,
    }
    _route(router, batch_path, ["POST"], handler, response_model=None)


# ---------------------------------------------------------------------------
# Route escape hatch + HTTP-layer helpers
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
    router.add_api_route(path, handler, methods=methods, **kwargs)


def _id_python_type(resource: type[BaseResource]) -> Any:
    """The Python type of the id field, for the ``{id}`` path parameter."""
    from resourcey.resource.base import _resolve_scalar_type

    id_field = resource.get_id_field()
    field = resource.model_fields[id_field]
    return _resolve_scalar_type(field.annotation)


def _filter_dependency(filter_cls: type[SearchFilter[Any]]) -> Callable[..., Any]:
    """Build a FastAPI dependency exposing each declared filter field as a query param.

    The declared ``SearchFilter`` class is the single source of truth for what
    is filterable: its ``model_fields`` (e.g. ``thread_id__eq``,
    ``text__contains``) become individual query parameters in the OpenAPI
    schema, typed from the field annotations. The dependency's signature is
    synthesised from those fields (one ``Query(default=None)`` parameter per
    field) so FastAPI unfolds them into separate OpenAPI parameters and
    coerces/validates each value (422 on a bad type) before the handler runs.
    """
    fields = list(filter_cls.model_fields.items())

    def dependency(**kwargs: Any) -> filter_cls:  # type: ignore[valid-type]
        return filter_cls(**{k: v for k, v in kwargs.items() if v is not None})

    # Replace the dependency's signature so FastAPI sees one typed,
    # Query-defaulted parameter per filter field. The annotation is set to
    # the real type object (not a string) so it resolves without forward
    # references even under ``from __future__ import annotations``.
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=Query(default=None),
            annotation=field.annotation,
        )
        for name, field in fields
    ]
    dependency.__signature__ = inspect.Signature(parameters=params)  # type: ignore[attr-defined]
    return dependency


def _resolve_filters(
    request: StarletteRequest,
    filter_cls: type[SearchFilter[Any]] | None,
    filters: SearchFilter[Any] | None,
    resource: type[BaseResource],
) -> SearchFilter[Any] | None:
    """Reject unknown ``field__op`` query params; return the validated filter.

    FastAPI collects the declared filter fields into ``filters`` (via the
    generated dependency) and surfaces them in the OpenAPI schema. It does
    not, however, reject *unknown* query parameters - they are silently
    ignored. To preserve the contract that a typo (an undeclared
    ``field__op``) surfaces as ``400 invalid_input`` rather than being
    dropped, any ``field__op`` query key not in the declared filter class is
    rejected here. When the resource declares no filter class, any
    ``field__op`` param is rejected outright.
    """
    filter_params = {k for k in request.query_params if "__" in k}
    if not filter_params:
        return None
    if filter_cls is None:
        raise InvalidInputError(
            f"Filter parameters {sorted(filter_params)} are not supported on "
            f"{resource.__name__}; it declares no search filter."
        )
    unknown = filter_params - set(filter_cls.model_fields)
    if unknown:
        raise InvalidInputError(f"Unknown filter parameters {sorted(unknown)}.")
    return filters


def _batch_edit_item_model(resource: type[BaseResource]) -> type[BaseModel]:
    """Build the request-body item model for ``batch-edit``: id + update fields.

    Combines the id field (typed) with the update model's fields so the JSON
    body validates each edit in one pass.
    """
    id_type = _id_python_type(resource)
    update_model = resource.get_update_model()
    update_fields = {
        name: (field.annotation, field) for name, field in update_model.model_fields.items()
    }
    id_field = resource.get_id_field()
    model = create_model(  # type: ignore[call-overload]
        f"{resource.__name__}BatchEditItem",
        **{id_field: (id_type, ...)},  # id is required on each edit
        **update_fields,
    )
    return cast("type[BaseModel]", model)


def _item_to_update_model(item: BaseModel, resource: type[BaseResource]) -> BaseModel:
    """Project a batch-edit item into an update-model instance (drop the id).

    Only fields the client explicitly supplied (not ``MISSING``) are carried
    across, preserving PATCH semantics.
    """
    update_model = resource.get_update_model()
    data = {
        name: getattr(item, name)
        for name in update_model.model_fields
        if hasattr(item, name) and getattr(item, name) is not MISSING
    }
    return update_model.model_validate(data)


# ---------------------------------------------------------------------------
# Response / error envelope helpers
# ---------------------------------------------------------------------------


def _json_response(
    payload: Any, context: dict[str, Any] | None, status_code: int = status.HTTP_200_OK
) -> JSONResponse:
    """Serialise a pydantic model / list / page with the secret context, as JSON."""
    body: Any
    if isinstance(payload, BaseModel):
        body = payload.model_dump(context=context)
    elif isinstance(payload, list):
        body = [
            item.model_dump(context=context) if isinstance(item, BaseModel) else item
            for item in payload
        ]
    else:
        body = payload
    return JSONResponse(content=jsonable_encoder(body), status_code=status_code)


def _http_date(value: datetime) -> str:
    """Format a datetime as an RFC 7231 IMF-fixdate (GMT)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return format_datetime(value.astimezone(UTC), usegmt=True)


def _cache_response_headers(header: CacheHeader) -> dict[str, str]:
    """Build the ``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires``
    response headers from a :class:`CacheHeader`'s non-``None`` fields."""
    headers: dict[str, str] = {}
    if header.etag is not None:
        headers["ETag"] = header.etag
    if header.updated_at is not None:
        headers["Last-Modified"] = _http_date(header.updated_at)
    if header.expire_at is not None:
        # max-age is the remaining freshness window (the strategy's expire_in,
        # computed moments ago). Rounding preserves the integer seconds clients
        # expect in Cache-Control.
        now = datetime.now(UTC)
        max_age = max(0, int((header.expire_at - now).total_seconds()))
        headers["Cache-Control"] = f"max-age={max_age}"
        headers["Expires"] = _http_date(header.expire_at)
    return headers


def _client_cache_header(request: StarletteRequest) -> CacheHeader:
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
    request: StarletteRequest,
    payload: Any,
    context: dict[str, Any] | None,
    header: CacheHeader | None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Serialise ``payload`` as JSON, applying cache headers and conditional
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
    if header is None or not header.has_any():
        return _json_response(payload, context, status_code)
    if request.method in ("GET", "HEAD") and not header.is_modified(_client_cache_header(request)):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers=_cache_response_headers(header),
        )
    response = _json_response(payload, context, status_code)
    response.headers.update(_cache_response_headers(header))
    return response


def register_error_handlers(app: FastAPI) -> None:
    """Register the consistent error envelope on a FastAPI app.

    Maps framework exceptions to the documented status + code:

    * ``NotFoundError`` -> 404 ``not_found``
    * ``InvalidInputError`` -> 400 ``invalid_input``
    * :class:`sqlalchemy.exc.IntegrityError` -> 409 ``conflict``
    * ``ServiceError`` -> 500 ``internal_error``
    * Pydantic validation failures keep FastAPI's 422 (its default handler).
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response("not_found", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(InvalidInputError)
    async def _invalid_input(_: Request, exc: InvalidInputError) -> JSONResponse:
        return _error_response("invalid_input", str(exc), status.HTTP_400_BAD_REQUEST)

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
