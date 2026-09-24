"""FastAPI route mounting for a ``v2`` resource's standard actions.

This is the ``v2`` transport layer's route half: it turns a
:class:`~resourcey.v2.core.resource.Resource` declaration into FastAPI routes
for the actions the resource supports, projecting each action's DTO result onto
the matching derived REST model.

:func:`register_routes` resolves the exposed resource once
(``resource.get_exposed_resource()``): a hidden resource registers no routes,
and the exposed resource drives the path, the derived models, the supported
actions, and the service dependency. There is no dependency builder in ``v2``
yet (issue #86), so the service dependency is read directly through
:func:`_service_dependency` — the single function that changes when a builder
lands.

Where the ``v2`` seams differ from ``v1``:

* ``v1``'s ``get_create_model`` / ``get_update_model`` / ``get_read_model`` are
  replaced by ``get_rest_models()`` -> :class:`~resourcey.v2.core.dto.RestModels`,
  so each action is mapped to the right shape explicitly (``create`` ->
  ``create_response``, so the one-time-reveal field survives).
* services return DTO instances, so every response is **projected** onto the
  REST model here (:func:`_project`), dropping the ``MISSING`` sentinel.
* ``v1``'s sort / filter / cache surface is out of scope (issue #79 and the
  cache follow-up), so search is ``limit`` + ``cursor`` only.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy.exc import IntegrityError

from resourcey.v2.core.dto import MISSING, RestModels
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import Action, NotFoundError, Service, ServiceError


def register_routes(
    app_or_router: FastAPI | APIRouter,
    resource: Resource[Any],
    *,
    prefix: str = "",
    tags: Sequence[str] | None = None,
) -> APIRouter:
    """Build an :class:`APIRouter` with the exposed resource's action routes and include it.

    Accepts a ``FastAPI`` app, an ``APIRouter``, or any object with
    ``include_router`` (duck-typed). Resolution order:

    1. ``exposed = resource.get_exposed_resource()``. When it is ``None`` the
       resource is internal-only and **no** routes are registered — the sole
       gate on exposure.
    2. ``exposed`` drives the path, the derived models, the supported actions,
       and the service dependency.

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

    router = APIRouter(tags=list(tags) if tags else [type(exposed).__name__])
    path = "/" + exposed.get_resource_path().lstrip("/")
    models = exposed.get_rest_models()
    id_field = exposed.get_id_field()
    id_type = _id_python_type(models, id_field)
    service_dep = _service_dependency(exposed)
    supported = exposed.get_supported_actions()

    # Static sub-paths (search / count / batch-read / batch-edit) are registered
    # before the ``{id}`` routes, otherwise ``batch-read`` would be captured as
    # an id value by the ``/{resource}/{id}`` route.
    if Action.SEARCH in supported:
        _add_search_route(router, path, models, service_dep)
    if Action.COUNT in supported:
        _add_count_route(router, path, service_dep)
    if Action.BATCH_READ in supported:
        _add_batch_read_route(router, path, models, id_type, service_dep)
    if Action.BATCH_EDIT in supported:
        _add_batch_edit_route(router, path, models, id_field, id_type, service_dep)
    if Action.CREATE in supported:
        _add_create_route(router, path, models, service_dep)
    if Action.READ in supported:
        _add_read_route(router, path, models, id_type, service_dep)
    if Action.UPDATE in supported:
        _add_update_route(router, path, models, id_type, service_dep)
    if Action.DELETE in supported:
        _add_delete_route(router, path, id_type, service_dep)

    app_or_router.include_router(router, prefix=_normalize_prefix(prefix))
    return router


# ---------------------------------------------------------------------------
# Service dependency (get_service_dependency -> FastAPI dependency)
# ---------------------------------------------------------------------------


def _service_dependency(resource: Resource[Any]) -> Callable[..., Any]:
    """Resolve the service dependency for ``resource``.

    ``v2`` has no ``DependencyBuilder`` yet (issue #86), so this reads the
    resource's own :meth:`~resourcey.v2.core.resource.Resource.get_service_dependency`.
    Keeping it behind one function means the builder, when it lands, changes
    only this.
    """
    return resource.get_service_dependency


# ---------------------------------------------------------------------------
# Route builders
# ---------------------------------------------------------------------------


def _add_create_route(router: APIRouter, path: str, models: RestModels, service_dep: Any) -> None:
    async def handler(payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        created = await service.create(payload)
        return _project(created, models.create_response)

    handler.__annotations__ = {"payload": models.create_request, "service": Service}
    _route(router, path, ["POST"], handler, status_code=status.HTTP_201_CREATED)


def _add_read_route(
    router: APIRouter, path: str, models: RestModels, id_type: Any, service_dep: Any
) -> None:
    async def handler(id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.read(id)
        return _project(found, models.read_response)

    handler.__annotations__ = {"id": id_type, "service": Service}
    _route(router, f"{path}/{{id}}", ["GET"], handler)


def _add_update_route(
    router: APIRouter, path: str, models: RestModels, id_type: Any, service_dep: Any
) -> None:
    async def handler(id, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        updated = await service.update(id, payload)
        return _project(updated, models.update_response)

    handler.__annotations__ = {"id": id_type, "payload": models.update_request, "service": Service}
    _route(router, f"{path}/{{id}}", ["PATCH"], handler)


def _add_delete_route(router: APIRouter, path: str, id_type: Any, service_dep: Any) -> None:
    async def handler(id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        await service.delete(id)
        return None

    handler.__annotations__ = {"id": id_type, "service": Service}
    _route(router, f"{path}/{{id}}", ["DELETE"], handler, status_code=status.HTTP_204_NO_CONTENT)


def _add_search_route(router: APIRouter, path: str, models: RestModels, service_dep: Any) -> None:
    """Register ``GET /{resource}`` — cursor-paginated search (no sort/filter yet).

    ``limit`` and ``cursor`` are the only query parameters: ordering is fixed to
    the identifier and filtering is out of scope (issue #79).
    """

    async def handler(  # type: ignore[no-untyped-def]
        limit=20,
        cursor=None,
        service=Depends(service_dep),  # noqa: B008
    ):
        page = await service.search(limit=limit, cursor=cursor)
        return _page_body(page, models.search_response)

    handler.__annotations__ = {"limit": int, "cursor": str | None, "service": Service}
    _route(router, path, ["GET"], handler)


def _add_count_route(router: APIRouter, path: str, service_dep: Any) -> None:
    """Register ``GET /{resource}/count`` — the matching row count."""

    async def handler(service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        return await service.count()

    handler.__annotations__ = {"service": Service}
    _route(router, f"{path}/count", ["GET"], handler)


def _add_batch_read_route(
    router: APIRouter, path: str, models: RestModels, id_type: Any, service_dep: Any
) -> None:
    async def handler(id=Query(default=[]), service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.batch_read(list(id))
        return [_project(item, models.search_response) for item in found]

    handler.__annotations__ = {"id": list[id_type], "service": Service}
    _route(router, f"{path}/batch-read", ["GET"], handler)


def _add_batch_edit_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    id_field: str,
    id_type: Any,
    service_dep: Any,
) -> None:
    item_model = _batch_edit_item_model(models.update_request, id_field, id_type)

    async def handler(payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        tuples = [
            (getattr(item, id_field), _item_to_update_model(item, models.update_request, id_field))
            for item in payload
        ]
        edited = await service.batch_edit(tuples)
        return [_project(item, models.update_response) for item in edited]

    handler.__annotations__ = {"payload": list[item_model], "service": Service}  # type: ignore[valid-type]
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


def _project(instance: Any, model: type[BaseModel]) -> dict[str, Any] | None:
    """Project a DTO instance onto a derived REST model (dropping ``MISSING``).

    The DTO is the internal type; the wire body is the projection. Fields the
    service left ``MISSING`` are omitted so the REST model's own defaults apply.
    ``None`` (a ``batch_read`` / ``batch_edit`` miss) projects to ``None``.
    """
    if instance is None:
        return None
    values = {name: value for name, value in vars(instance).items() if value is not MISSING}
    return model.model_validate(values).model_dump(mode="json")


def _page_body(page: Any, search_response: type[BaseModel]) -> dict[str, Any]:
    """Serialise a :class:`~resourcey.v2.core.service.Page` with projected items."""
    return {
        "items": [_project(item, search_response) for item in page.items],
        "limit": page.limit,
        "next_cursor": page.next_cursor,
    }


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


def _item_to_update_model(
    item: BaseModel, update_request: type[BaseModel], id_field: str
) -> BaseModel:
    """Project a batch-edit item into an update-request instance (dropping the id).

    Only fields the client explicitly supplied (``exclude_unset``) are carried
    across, preserving PATCH semantics.
    """
    data = {k: v for k, v in item.model_dump(exclude_unset=True).items() if k != id_field}
    return update_request.model_validate(data)


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
