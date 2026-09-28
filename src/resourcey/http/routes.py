"""FastAPI route mounting for a resource's standard actions.

This is the transport layer's route half: it turns a
:class:`~resourcey.core.resource.Resource` declaration into FastAPI routes
for the actions the resource supports, projecting each action's DTO result onto
the matching derived REST model.

:func:`register_routes` resolves the exposed resource once
(``resource.get_exposed_resource()``): a hidden resource registers no routes,
and the exposed resource drives the path, the derived models, the supported
actions, and the service dependency. The per-request service dependency is
built through the configured :class:`~resourcey.http.dependency_builder.DependencyBuilder`
(issue #86): the builder is resolved on the **exposed** resource, so a
projection's wrapped service is the projection's, not the hidden inner
resource's.

Where the seams differ from the earlier iteration:

* ``v1``'s ``get_create_model`` / ``get_update_model`` / ``get_read_model`` are
  replaced by ``get_rest_models()`` -> :class:`~resourcey.core.dto.RestModels`,
  so each action is mapped to the right shape explicitly (``create`` ->
  ``create_response``, so the one-time-reveal field survives).
* services return DTO instances, so every response is **projected** onto the
  REST model here (:func:`_project`), dropping the ``MISSING`` sentinel.
* filtering is back (issue #79): search and count accept ``<field>__<op>``
  query params, validated against the exposed resource's filter surface (a
  declared :meth:`~resourcey.core.resource.Resource.get_search_filter_type`,
  else derived from the read model). Sorting is back too (issue #97): search
  accepts ``sort`` / ``desc``, validated against the exposed resource's
  :meth:`~resourcey.core.resource.Resource.get_sortable_fields`.
* caching is back (issue #92): the exposed resource's
  :meth:`~resourcey.core.resource.Resource.get_cache_strategy` drives
  ``ETag`` / ``Last-Modified`` / ``Cache-Control`` / ``Expires`` headers, and a
  conditional ``GET`` short-circuits to ``304 Not Modified``.

This module imports no code outside the framework.
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
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, create_model
from sqlalchemy.exc import IntegrityError

from resourcey.cache.cache_header import CacheHeader
from resourcey.core.dto import RestModels, request_to_dto
from resourcey.core.errors import ConflictError, InvalidInputError, UnsupportedFilterError
from resourcey.core.resource import Resource
from resourcey.core.service import (
    DEFAULT_LIMIT,
    Action,
    Create,
    Delete,
    ForbiddenError,
    NotFoundError,
    Service,
    ServiceError,
    Update,
    normalize_actions,
)
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.util.missing import MISSING
from resourcey.util.naming import humanize, pluralize
from resourcey.util.search_filter import SEPARATOR, SearchFilter, build_filter

T = TypeVar("T", bound=BaseModel)


def register_routes(
    app_or_router: FastAPI | APIRouter,
    resource: Resource[Any, Any],
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
    :class:`~resourcey.http.dependency_builder.OpenDependencyBuilder`. It
    is resolved on ``exposed``, so a projection's wrapped service is the
    projection's.

    The ``{resource}`` path segment is the plural, lower-case, kebab-case name
    from ``exposed.get_resource_path()``; sub-paths use dashes (``batch-read``,
    ``batch-edit``, ``count``). ``tags`` defaults to that path humanized
    (``"threads"`` -> ``"Threads"``, ``"api-keys"`` -> ``"Api Keys"``), so
    operations group by resource rather than by the *framework* class that
    happens to implement it — a model-first ``SqlResource(Thread, ...)`` and a
    ``MessageResource(Message, ...)`` land in distinct groups, and a
    ``ResourceView`` does not collapse into a single ``ResourceView`` bucket. A
    route is only added if none already exists at that path + method on the
    target router — a developer who registers a custom route first keeps it
    (escape hatch). Returns the built :class:`APIRouter`.
    """
    exposed = resource.get_exposed_resource()
    if exposed is None:
        # Hidden resource: no routes. The (empty) router's tag is irrelevant.
        return APIRouter(tags=list(tags) if tags else [type(resource).__name__])

    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()
    resource_name = _resource_display_name(exposed)
    router = APIRouter(
        tags=list(tags) if tags else [_default_tag(exposed)],
    )
    path = "/" + exposed.get_resource_path().lstrip("/")
    models = exposed.get_rest_models()
    dto_model = exposed.get_dto_type()
    id_field = exposed.get_id_field()
    id_type = _id_python_type(models, id_field)
    service_dep = _service_dependency(exposed, builder)
    # An authenticating builder (issue #131) supplies a dependency the transport
    # adds to every route, so the security scheme appears in the OpenAPI
    # operation; a builder without one (the default) contributes nothing.
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None
    supported = normalize_actions(exposed.get_supported_actions())
    strategy = exposed.get_cache_strategy()

    # Static sub-paths (search / count / batch-read / batch-edit) are registered
    # before the ``{id}`` routes, otherwise ``batch-read`` would be captured as
    # an id value by the ``/{resource}/{id}`` route.
    if Action.SEARCH in supported:
        _add_search_route(
            router, path, models, exposed, service_dep, strategy, resource_name, route_deps
        )
    if Action.COUNT in supported:
        _add_count_route(router, path, exposed, service_dep, strategy, resource_name, route_deps)
    if Action.BATCH_READ in supported:
        _add_batch_read_route(
            router, path, models, id_type, service_dep, strategy, resource_name, route_deps
        )
    if Action.BATCH_EDIT in supported:
        _add_batch_edit_route(
            router,
            path,
            models,
            dto_model,
            id_field,
            id_type,
            supported,
            service_dep,
            strategy,
            resource_name,
            route_deps,
        )
    if Action.CREATE in supported:
        _add_create_route(
            router, path, models, dto_model, service_dep, strategy, resource_name, route_deps
        )
    if Action.READ in supported:
        _add_read_route(
            router, path, models, id_type, service_dep, strategy, resource_name, route_deps
        )
    if Action.UPDATE in supported:
        _add_update_route(
            router,
            path,
            models,
            dto_model,
            id_field,
            id_type,
            service_dep,
            strategy,
            resource_name,
            route_deps,
        )
    if Action.DELETE in supported:
        _add_delete_route(router, path, id_type, service_dep, resource_name, route_deps)

    app_or_router.include_router(router, prefix=_normalize_prefix(prefix))
    return router


# ---------------------------------------------------------------------------
# Service dependency (get_service -> FastAPI dependency)
# ---------------------------------------------------------------------------


def _service_dependency(
    resource: Resource[T, Any], builder: DependencyBuilder
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


def _filter_surface(resource: Resource[Any, Any]) -> dict[str, tuple[Any, frozenset[str]]]:
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
            # ``contains`` / ``in`` take a string on the wire: ``contains`` is a
            # substring test, and ``in`` is a comma-separated value list.
            value_annotation = str if op in ("contains", "in") else annotation
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
        if not (sep and attribute):
            continue
        annotation = surface.get(attribute, (None, frozenset()))[0]
        clauses.append((attribute, op, _coerce_filter_value(op, value, annotation)))
    if not clauses:
        return None
    try:
        return build_filter(clauses)
    except ValidationError as exc:
        # A leaf's own validation (e.g. an ``in`` set over the cap) is bad input,
        # not a server fault.
        raise InvalidInputError(f"Invalid filter value: {exc}") from exc


def _coerce_filter_value(op: str, value: Any, annotation: Any) -> Any:
    """Coerce a wire filter value to the field's type.

    ``in`` arrives as a comma-separated string (the transport types it as
    ``str``); each item must be coerced to the column's type (UUID, int, ...)
    before the ``IN`` predicate binds it, so a typed set filter accepts the same
    string form a single-value filter already does. Every other operator is
    already coerced by FastAPI via its typed query parameter.

    A malformed item is an ``InvalidInputError`` (``400``), matching the ``422``
    FastAPI gives a bad single-value param, rather than escaping as a ``500``.
    """
    if op != "in" or not isinstance(value, str):
        return value
    items = [part for part in (p.strip() for p in value.split(",")) if part]
    if annotation is None:
        return items
    adapter = TypeAdapter(annotation)
    try:
        return [adapter.validate_python(item) for item in items]
    except ValidationError as exc:
        raise InvalidInputError(f"Invalid value for {op!r} filter: {exc}") from exc


# ---------------------------------------------------------------------------
# Route builders
# ---------------------------------------------------------------------------


def _resource_display_name(exposed: Resource[Any, Any]) -> str:
    """A human-readable name for ``exposed``, from its DTO type.

    Used to make each operation's summary / description name the resource it
    acts on (e.g. "Create Thread"). The declaration's trailing ``DTO``
    convention suffix is dropped so ``ThreadDTO`` reads as ``Thread`` (matching
    the SQL backend's inferred ``Thread``). Falls back to the resource path when
    the DTO type has no usable name.
    """
    name = getattr(exposed.get_dto_type(), "__name__", "")
    if name.endswith("DTO") and len(name) > 3:
        name = name[: -len("DTO")]
    if name:
        return name
    return exposed.get_resource_path()


def _default_tag(exposed: Resource[Any, Any]) -> str:
    """The default OpenAPI tag for ``exposed``: its collection path, humanized.

    ``type(exposed).__name__`` is a poor tag for a model-first resource — every
    bare ``SqlResource(Thread, ...)`` would share the one ``"SqlResource"``
    bucket, and every ``ResourceView`` would collapse into ``"ResourceView"``.
    The REST path is the identifier the routes actually live under and is
    already the plural, kebab-case collection name, so humanizing it groups
    operations the way a caller navigates them: ``threads`` -> ``Threads``,
    ``messages`` -> ``Messages``, ``api-keys`` -> ``Api Keys``. It falls back to
    the framework class name only if the path is somehow empty.
    """
    return humanize(exposed.get_resource_path()) or type(exposed).__name__


def _operation_metadata(action: Action, resource_name: str) -> tuple[str, str]:
    """The ``(summary, description)`` for one generated operation.

    Every handler is a closure, so FastAPI would otherwise fall back to the
    bare summary "Handler" and emit no description for every route. These name
    the action and the resource so the generated OpenAPI is self-describing.

    ``{name}`` is the singular resource name and ``{plural}`` its plural, so the
    read-side actions can talk about the collection and the rest about one
    object.
    """
    templates: dict[Action, tuple[str, str]] = {
        Action.CREATE: (
            "Create {name}",
            "Create a new {name} and return the created object.",
        ),
        Action.READ: (
            "Read {name}",
            "Fetch a single {name} by its identifier.",
        ),
        Action.UPDATE: (
            "Update {name}",
            "Partially update an existing {name} by its identifier and return the updated object.",
        ),
        Action.DELETE: (
            "Delete {name}",
            "Delete a {name} by its identifier.",
        ),
        Action.SEARCH: (
            "Search {name}",
            "List {plural} using cursor pagination, optional sorting, and optional field filters.",
        ),
        Action.COUNT: (
            "Count {name}",
            "Count the {plural} matching the given field filters.",
        ),
        Action.BATCH_READ: (
            "Batch read {name}",
            "Fetch several {plural} by identifier in one request. "
            "The result is positionally aligned with the requested ids.",
        ),
        Action.BATCH_EDIT: (
            "Batch edit {name}",
            "Create, update, and delete {plural} in one request. "
            "The result is positionally aligned with the submitted edits.",
        ),
    }
    summary, description = templates[action]
    values = {"name": resource_name, "plural": pluralize(resource_name)}
    return summary.format(**values), description.format(**values)


def _add_create_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        created = await service.create(request_to_dto(dto_model, payload))
        context = service.serialization_context()
        projected = _project(created, models.create_response, context)
        header = _header_for(strategy, [projected], context, service)
        return _cached_json_response(
            request, _dump(projected, context), header, status.HTTP_201_CREATED
        )

    handler.__annotations__ = {
        "request": Request,
        "payload": models.create_request,
        "service": Service,
    }
    summary, description = _operation_metadata(Action.CREATE, resource_name)
    _route(
        router,
        path,
        ["POST"],
        handler,
        dependencies=route_deps,
        status_code=status.HTTP_201_CREATED,
        summary=summary,
        description=description,
    )


def _add_read_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    async def handler(request, id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.read(id)
        context = service.serialization_context()
        projected = _project(found, models.read_response, context)
        header = _header_for(strategy, [projected], context, service)
        return _cached_json_response(request, _dump(projected, context), header)

    handler.__annotations__ = {"request": Request, "id": id_type, "service": Service}
    summary, description = _operation_metadata(Action.READ, resource_name)
    _route(
        router,
        f"{path}/{{id}}",
        ["GET"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


def _add_update_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    id_field: str,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    async def handler(request, id, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        updated = await service.update(_update_dto(dto_model, id_field, id, payload))
        context = service.serialization_context()
        projected = _project(updated, models.update_response, context)
        header = _header_for(strategy, [projected], context, service)
        return _cached_json_response(request, _dump(projected, context), header)

    handler.__annotations__ = {
        "request": Request,
        "id": id_type,
        "payload": models.update_request,
        "service": Service,
    }
    summary, description = _operation_metadata(Action.UPDATE, resource_name)
    _route(
        router,
        f"{path}/{{id}}",
        ["PATCH"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


def _add_delete_route(
    router: APIRouter,
    path: str,
    id_type: Any,
    service_dep: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    async def handler(id, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        await service.delete(id)
        return None

    handler.__annotations__ = {"id": id_type, "service": Service}
    summary, description = _operation_metadata(Action.DELETE, resource_name)
    _route(
        router,
        f"{path}/{{id}}",
        ["DELETE"],
        handler,
        dependencies=route_deps,
        status_code=status.HTTP_204_NO_CONTENT,
        summary=summary,
        description=description,
    )


def _add_search_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    exposed: Resource[Any, Any],
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    """Register ``GET /{resource}`` — cursor-paginated, filterable, sortable search.

    ``limit`` and ``cursor`` paginate; ``sort`` / ``desc`` order the page
    (resolved by the exposed resource's
    :meth:`~resourcey.core.resource.Resource.resolve_sort_order`, an unknown
    or non-sortable field -> ``400``); declared ``<field>__<op>`` query params
    are collected into a standard :class:`SearchFilter` and pushed down. The
    filter surface (fields + operators) comes from the exposed resource's
    :meth:`~resourcey.core.resource.Resource.get_filter_operators` (or a
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
        limit=DEFAULT_LIMIT,
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
        context = service.serialization_context()
        body, items = _page_body(page, models.search_response, context)
        header = _header_for(strategy, items, context, service)
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
    summary, description = _operation_metadata(Action.SEARCH, resource_name)
    _route(
        router,
        path,
        ["GET"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


def _add_count_route(
    router: APIRouter,
    path: str,
    exposed: Resource[Any, Any],
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
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
        effective = _private_strategy(strategy, service)
        if effective is not None:
            candidate = effective.count_cache_header(total, filters)
            header = candidate if candidate.has_any() else None
        return _cached_json_response(request, total, header)

    handler.__annotations__ = {"request": Request, "values": dict, "service": Service}
    summary, description = _operation_metadata(Action.COUNT, resource_name)
    _route(
        router,
        f"{path}/count",
        ["GET"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


def _add_batch_read_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    id_type: Any,
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    async def handler(request, id=Query(default=[]), service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.batch_read(list(id))
        context = service.serialization_context()
        items = [_project(item, models.search_response, context) for item in found]
        header = _header_for(strategy, items, context, service)
        return _cached_json_response(request, _dump(items, context), header)

    handler.__annotations__ = {"request": Request, "id": list[id_type], "service": Service}
    summary, description = _operation_metadata(Action.BATCH_READ, resource_name)
    _route(
        router,
        f"{path}/batch-read",
        ["GET"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


def _add_batch_edit_route(
    router: APIRouter,
    path: str,
    models: RestModels,
    dto_model: type[BaseModel],
    id_field: str,
    id_type: Any,
    supported: frozenset[Action],
    service_dep: Any,
    strategy: Any,
    resource_name: str,
    route_deps: list[Any] | None = None,
) -> None:
    """Register ``POST /{resource}/batch-edit`` — mixed create / update / delete.

    The body is a list discriminated by ``kind`` (``Create`` / ``Update`` /
    ``Delete``), so one batch can create, update, *and* delete. Each item is
    folded into the matching :class:`~resourcey.core.service.Edit` node and
    the results align positionally with the input (a delete, or a miss, is
    ``None``).

    ``create`` / ``delete`` are only admitted when the resource *declares* those
    actions (``supported``), so a batch cannot reach an action the resource
    never exposed; the corresponding ``kind`` is absent from the body schema and
    an unexpected one is rejected ``422``.
    """
    allow_create = Action.CREATE in supported
    allow_update = Action.UPDATE in supported
    allow_delete = Action.DELETE in supported
    body_model = _batch_edit_body(
        models.create_request,
        models.update_request,
        id_field,
        id_type,
        allow_create,
        allow_update,
        allow_delete,
    )

    async def handler(request, payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        edits = [_batch_edit_node(item, dto_model, id_field) for item in payload]
        edited = await service.batch_edit(edits)
        context = service.serialization_context()
        items = [
            _project_edit_result(edit, result, models, context)
            for edit, result in zip(edits, edited, strict=True)
        ]
        header = _header_for(strategy, items, context, service)
        return _cached_json_response(request, _dump(items, context), header)

    handler.__annotations__ = {
        "request": Request,
        "payload": list[body_model],  # type: ignore[valid-type]
        "service": Service,
    }
    summary, description = _operation_metadata(Action.BATCH_EDIT, resource_name)
    _route(
        router,
        f"{path}/batch-edit",
        ["POST"],
        handler,
        dependencies=route_deps,
        summary=summary,
        description=description,
    )


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
    dependencies = kwargs.pop("dependencies", None)
    router.add_api_route(
        path,
        handler,
        methods=methods,
        response_model=None,
        dependencies=dependencies,
        **kwargs,
    )


def _project(
    instance: Any, model: type[BaseModel], context: dict[str, Any] | None = None
) -> BaseModel | None:
    """Project a DTO instance onto a derived REST model (dropping ``MISSING``).

    The DTO is the internal type; the wire body is the projection. Fields the
    service left ``MISSING`` are omitted so the REST model's own defaults apply.
    ``None`` (a ``batch_read`` / ``batch_edit`` miss) projects to ``None``.

    ``context`` is the service's serialization context; it is threaded into
    ``model_validate`` so a secret-bearing field loads per the convention
    (decrypted under an ``encryption_service``, plaintext under
    ``expose_secrets``).

    Returns the validated model instance (not a dict) so the cache strategy can
    hash exactly the representation that will be serialised.
    """
    if instance is None:
        return None
    values = {name: value for name, value in vars(instance).items() if value is not MISSING}
    return model.model_validate(values, context=context)


def _dump(projected: Any, context: dict[str, Any] | None = None) -> Any:
    """Serialise a projected model / list / scalar to a JSON-ready value.

    ``context`` is the service's serialization context, threaded into
    ``model_dump`` so the wire body reflects it rather than always redacting.
    """
    if isinstance(projected, BaseModel):
        return projected.model_dump(mode="json", context=context)
    if isinstance(projected, list):
        return [_dump(item, context) for item in projected]
    return projected


def _page_body(
    page: Any, search_response: type[BaseModel], context: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[Any]]:
    """Serialise a :class:`~resourcey.core.service.Page`; return body + projected items.

    The projected items are returned alongside the body so the cache strategy
    hashes the same representations the response carries.
    """
    items = [_project(item, search_response, context) for item in page.items]
    return {
        "items": [_dump(item, context) for item in items],
        "limit": page.limit,
        "next_cursor": page.next_cursor,
    }, items


# ---------------------------------------------------------------------------
# HTTP caching (ETag / Last-Modified / Cache-Control + 304 short-circuit)
# ---------------------------------------------------------------------------


def _response_is_private(service: Any) -> bool:
    """Whether ``service`` reports its responses as caller-scoped."""
    fn = getattr(service, "response_is_private", None)
    return bool(fn()) if callable(fn) else False


def _private_strategy(strategy: Any, service: Any) -> Any:
    """``strategy`` with privacy forced on when ``service`` is caller-scoped.

    A principal-narrowed resource (an ``Owner`` policy, or any resolver / policy
    that scopes to the caller) must not let a shared cache store or revalidate
    its response under a validator another caller could present. Forcing
    ``private`` onto the strategy makes the emitted ``Cache-Control`` carry
    ``private``. A strategy with no ``with_private`` (the ``core`` placeholder)
    is left alone — it produces no validator, so there is nothing to protect.
    """
    if strategy is None or not _response_is_private(service):
        return strategy
    with_private = getattr(strategy, "with_private", None)
    return with_private(True) if callable(with_private) else strategy


def _header_for(
    strategy: Any,
    items: list[Any],
    context: dict[str, Any] | None = None,
    service: Any = None,
) -> CacheHeader | None:
    """Compute the cache header for ``items`` via the resource's strategy.

    ``None`` when the resource declares no strategy or the strategy yields
    nothing (no validators and no freshness), so the response is uncached.
    ``context`` is the service's serialization context, so a secret-bearing ETag
    hashes exactly the bytes the response carries. ``service`` supplies the
    per-request privacy fact (a caller-scoped response forces ``private`` onto
    the header), so a shared cache cannot replay it across principals.
    """
    if strategy is None:
        return None
    effective = _private_strategy(strategy, service) if service is not None else strategy
    header = effective.get_cache_header(items, context=context)
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
    allow_create: bool,
    allow_update: bool,
    allow_delete: bool,
) -> Any:
    """Build the ``batch-edit`` body: a ``kind``-discriminated union of edits.

    The wire shapes match :class:`~resourcey.core.service.Create`,
    :class:`~resourcey.core.service.Update`, and
    :class:`~resourcey.core.service.Delete`:

    * ``{kind: "Create", item: <create_request>}``
    * ``{kind: "Update", item: {<id_field>, ...update_request fields}}``
    * ``{kind: "Delete", <id_field>: <id>}``

    The update item carries the identifier because the path does not; a create
    item is the create request verbatim (a server-generated id is absent, a
    natural key is client-supplied). ``Delete`` carries the identifier directly
    rather than nesting it under ``item``: there is no payload to nest, and
    mirroring the other nodes' ``item`` wrapper would only add an empty object.

    ``allow_create`` / ``allow_update`` / ``allow_delete`` narrow the union to
    the actions the resource declares, so a batch can never reach an action the
    resource does not expose: the corresponding ``kind`` is absent from the body
    schema and an unexpected one is rejected before a node is ever built. A
    ``kind`` with no allowed member would make an empty union, so when none of
    the three is allowed ``batch_edit`` itself was dropped by
    :func:`~resourcey.core.service.normalize_actions` and this function is
    never reached.
    """
    members: list[Any] = []
    if allow_create:
        members.append(
            create_model(
                f"{create_request.__name__}CreateEdit",
                kind=(Literal["Create"], ...),
                item=(create_request, ...),
            )
        )
    if allow_update:
        update_fields = {
            name: (field.annotation, field) for name, field in update_request.model_fields.items()
        }
        update_item = create_model(  # type: ignore[call-overload]
            f"{update_request.__name__}BatchUpdateItem",
            **{id_field: (id_type, ...)},  # the id is required on each update
            **update_fields,
        )
        members.append(
            create_model(
                f"{update_request.__name__}UpdateEdit",
                kind=(Literal["Update"], ...),
                item=(update_item, ...),
            )
        )
    if allow_delete:
        members.append(
            create_model(  # type: ignore[call-overload]
                f"{create_request.__name__}DeleteEdit",
                kind=(Literal["Delete"], ...),
                **{id_field: (id_type, ...)},
            )
        )
    if not members:
        raise ServiceError(
            "batch-edit body has no members: batch_edit must be dropped when no write "
            "action is exposed (normalize_actions)."
        )
    edits = members[0]
    for member in members[1:]:
        edits = edits | member
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
    edit: Any, result: BaseModel | None, models: RestModels, context: dict[str, Any] | None = None
) -> BaseModel | None:
    """Project a batch-edit result onto the shape for its edit.

    A delete yields ``None`` (nothing to return); a create projects onto the
    create response (so a one-time-reveal field survives) and an update onto the
    update response. A miss (``result`` is ``None``) stays ``None``.
    """
    if isinstance(edit, Delete) or result is None:
        return None
    if isinstance(edit, Create):
        return _project(result, models.create_response, context)
    return _project(result, models.update_response, context)


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

    Maps the exceptions the framework has today to the documented status + code:

    * ``NotFoundError`` -> 404 ``not_found``
    * ``ForbiddenError`` -> 403 ``forbidden``
    * ``InvalidInputError`` -> 400 ``invalid_input``
    * ``UnsupportedFilterError`` -> 501 ``unsupported_filter``
    * ``ConflictError`` / ``IntegrityError`` -> 409 ``conflict``
    * ``ServiceError`` -> 500 ``internal_error``
    * Pydantic validation failures keep FastAPI's 422 (its default handler).

    The wider ``ResourceyError`` hierarchy (issue #83) extends this same
    function.
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response("not_found", str(exc), status.HTTP_404_NOT_FOUND)

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        return _error_response("forbidden", str(exc), status.HTTP_403_FORBIDDEN)

    @app.exception_handler(InvalidInputError)
    async def _invalid_input(_: Request, exc: InvalidInputError) -> JSONResponse:
        return _error_response("invalid_input", str(exc), status.HTTP_400_BAD_REQUEST)

    @app.exception_handler(UnsupportedFilterError)
    async def _unsupported_filter(_: Request, exc: UnsupportedFilterError) -> JSONResponse:
        return _error_response("unsupported_filter", str(exc), status.HTTP_501_NOT_IMPLEMENTED)

    @app.exception_handler(ConflictError)
    async def _conflict_error(_: Request, exc: ConflictError) -> JSONResponse:
        return _error_response("conflict", str(exc), status.HTTP_409_CONFLICT)

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
