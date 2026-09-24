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
* ``v1``'s sort / filter surface is out of scope (issue #79), so search is
  ``limit`` + ``cursor`` only.
* caching is back (issue #92): the exposed resource's
  :meth:`~resourcey.v2.core.resource.Resource.get_cache_strategy` drives
  ``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires`` headers, and a
  conditional ``GET`` short-circuits to ``304 Not Modified``.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy.exc import IntegrityError

from resourcey.v2.cache.cache_header import CacheHeader
from resourcey.v2.core.dto import RestModels, request_to_dto
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Action, NotFoundError, Service, ServiceError
from resourcey.v2.http.dependency_builder import DefaultDependencyBuilder, DependencyBuilder
from resourcey.v2.util.missing import MISSING

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
        _add_search_route(router, path, models, service_dep, strategy)
    if Action.COUNT in supported:
        _add_count_route(router, path, service_dep, strategy)
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
) -> Callable[..., AsyncIterator[Service[T]]]:
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
    return cast("Callable[..., AsyncIterator[Service[T]]]", dependency)


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
    service_dep: Any,
    strategy: Any,
) -> None:
    """Register ``GET /{resource}`` — cursor-paginated search (no sort/filter yet).

    ``limit`` and ``cursor`` are the only query parameters: ordering is fixed to
    the identifier and filtering is out of scope (issue #79).
    """

    async def handler(  # type: ignore[no-untyped-def]
        request,
        limit=20,
        cursor=None,
        service=Depends(service_dep),  # noqa: B008
    ):
        page = await service.search(limit=limit, cursor=cursor)
        body, items = _page_body(page, models.search_response)
        header = _header_for(strategy, items)
        return _cached_json_response(request, body, header)

    handler.__annotations__ = {
        "request": Request,
        "limit": int,
        "cursor": str | None,
        "service": Service,
    }
    _route(router, path, ["GET"], handler)


def _add_count_route(
    router: APIRouter,
    path: str,
    service_dep: Any,
    strategy: Any,
) -> None:
    """Register ``GET /{resource}/count`` — the matching row count."""

    async def handler(request, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        total = await service.count()
        header: CacheHeader | None = None
        if strategy is not None:
            candidate = strategy.count_cache_header(total)
            header = candidate if candidate.has_any() else None
        return _cached_json_response(request, total, header)

    handler.__annotations__ = {"request": Request, "service": Service}
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
    item_model = _batch_edit_item_model(models.update_request, id_field, id_type)

    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        edits = [_item_to_update_dto(item, dto_model) for item in payload]
        edited = await service.batch_edit(edits)
        items = [_project(item, models.update_response) for item in edited]
        header = _header_for(strategy, items)
        return _cached_json_response(request, _dump(items), header)

    handler.__annotations__ = {
        "request": Request,
        "payload": list[item_model],  # type: ignore[valid-type]
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
    if header.expire_at is not None:
        # max-age is the remaining freshness window (the strategy's expire_in,
        # computed moments ago). Rounding preserves the integer seconds clients
        # expect in Cache-Control.
        now = datetime.now(UTC)
        max_age = max(0, int((header.expire_at - now).total_seconds()))
        headers["Cache-Control"] = f"max-age={max_age}"
        headers["Expires"] = _http_date(header.expire_at)
    elif has_validator:
        # Validators with no freshness window: force revalidation on every use.
        # Without a Cache-Control directive a browser falls back to heuristic
        # freshness and serves from cache without ever echoing the validator
        # back, so the conditional-request path (and 304s) never fires.
        headers["Cache-Control"] = "no-cache"
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


def _batch_edit_item_model(
    update_request: type[BaseModel], id_field: str, id_type: Any
) -> type[BaseModel]:
    """Build the request-body item model for ``batch-edit``: id + update fields.

    Combines the typed identifier with the update model's fields so the JSON
    body validates each edit in one pass.
    """
    update_fields = {
        name: (field.annotation, field) for name, field in update_request.model_fields.items()
    }
    model = create_model(  # type: ignore[call-overload]
        f"{update_request.__name__}BatchEditItem",
        **{id_field: (id_type, ...)},  # id is required on each edit
        **update_fields,
    )
    return cast("type[BaseModel]", model)


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


def _item_to_update_dto(item: BaseModel, dto_model: type[BaseModel]) -> BaseModel:
    """Build a batch-edit DTO from an item that already carries its own id."""
    return request_to_dto(dto_model, item)


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
    * ``IntegrityError`` -> 409 ``conflict``
    * ``ServiceError`` -> 500 ``internal_error``
    * Pydantic validation failures keep FastAPI's 422 (its default handler).

    The wider ``ResourceyError`` hierarchy (issue #83) extends this same
    function.
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response("not_found", str(exc), status.HTTP_404_NOT_FOUND)

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
