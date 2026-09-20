"""The auto-generated REST service for a resource.

``ResourceService`` is the central deliverable of issue #2. Bound to a
``BaseResource`` subclass, it exposes the seven standard actions as
overridable async methods that each take an ``AsyncSession``. It contains
logic (validation orchestration, error mapping, PATCH merge, pagination
assembly, sort validation) and delegates data access to a
:class:`~resourcey.resource.repository.ResourceRepository`.

The service is usable independently of HTTP — call its methods directly with
an ``AsyncSession``. :meth:`ResourceService.register` is a thin convenience
that mounts the seven actions onto a FastAPI app or router.

Auth is explicitly deferred to #4: an overridable :meth:`authorize` hook is
a no-op by default and is called before every action, so a later PR can
fill it in without reworking the action methods or the route wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from resourcey.resource.errors import InvalidInputError, NotFoundError
from resourcey.resource.missing import MISSING
from resourcey.resource.repository import ResourceRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of search results with pagination metadata."""

    items: list[T]
    total: int
    limit: int
    offset: int


# Default pagination bounds. ``limit`` is capped so a client cannot request
# an unbounded scan; ``offset`` must be non-negative.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


class ResourceService:
    """The service object exposing the seven standard resource actions.

    Constructed from a ``BaseResource`` subclass (and, optionally, a
    ``ResourceRepository`` subclass to override the default — escape hatch).
    Resolves the create / update / read models and id field from the
    resource and caches the repository instance. Each action is an
    overridable async method taking an ``AsyncSession``.
    """

    def __init__(
        self,
        resource: type[BaseResource],
        *,
        repository_cls: type[ResourceRepository] | None = None,
        serialization_context: dict[str, Any] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.resource = resource
        self.create_model = resource.get_create_model()
        self.update_model = resource.get_update_model()
        self.read_model = resource.get_read_model()
        self.id_field = resource.get_id_field()
        self.repository = (repository_cls or ResourceRepository)(resource)
        self._serialization_context = serialization_context
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The pydantic serialization context used for secret fields.

        Flows to ``model_dump`` / ``model_validate`` in the repository so
        ``SecretStr`` fields encrypt on write and decrypt on read. ``None``
        means no context (secrets redact on dump — only appropriate when the
        resource has no secret fields). Override or pass
        ``serialization_context`` at construction to supply an
        ``encryption_service`` / ``expose_secrets`` context.
        """
        return self._serialization_context

    def _ctx(self) -> dict[str, Any] | None:
        return self.serialization_context()

    # ------------------------------------------------------------------
    # Authorisation hook (deferred to #4)
    # ------------------------------------------------------------------

    async def authorize(self, session: AsyncSession, action: str, **context: Any) -> None:
        """Overridable authorisation hook. A no-op by default (auth -> #4).

        Every action calls it before doing work, so #4 can implement real
        permission checks without touching the action methods or route
        wiring. Raise to deny.
        """

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    async def create(self, session: AsyncSession, payload: BaseModel) -> Any:
        """Validate via the create model, persist, return the read model (HTTP 201)."""
        await self.authorize(session, "create", payload=payload)
        return await self.repository.insert(session, payload, context=self._ctx())

    async def read(self, session: AsyncSession, id: Any) -> Any:  # noqa: A002
        """Fetch; raise :class:`NotFoundError` (-> 404) if absent."""
        await self.authorize(session, "read", id=id)
        result = await self.repository.get_by_id(session, id, context=self._ctx())
        if result is None:
            raise NotFoundError(self.resource.__name__, id)
        return result

    async def update(self, session: AsyncSession, id: Any, payload: BaseModel) -> Any:  # noqa: A002
        """Validate via the PATCH update model, apply; raise ``NotFoundError`` if absent."""
        await self.authorize(session, "update", id=id, payload=payload)
        result = await self.repository.update_by_id(session, id, payload, context=self._ctx())
        if result is None:
            raise NotFoundError(self.resource.__name__, id)
        return result

    async def delete(self, session: AsyncSession, id: Any) -> None:  # noqa: A002
        """Delete; raise ``NotFoundError`` if absent. Returns no body (HTTP 204)."""
        await self.authorize(session, "delete", id=id)
        deleted = await self.repository.delete_by_id(session, id)
        if not deleted:
            raise NotFoundError(self.resource.__name__, id)

    async def search(
        self,
        session: AsyncSession,
        *,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
        sort: list[str] | None = None,
        filters: SearchFilter[Any] | None = None,
    ) -> Page[Any]:
        """Search with pagination, sort, and optional filters; return a :class:`Page`."""
        await self.authorize(
            session, "search", limit=limit, offset=offset, sort=sort, filters=filters
        )
        limit, offset = self._validate_pagination(limit, offset)
        sort_parsed = self._parse_sort(sort)
        items = await self.repository.search(
            session,
            limit=limit,
            offset=offset,
            sort=sort_parsed,
            filters=filters,
            context=self._ctx(),
        )
        total = await self.repository.count(session, filters=filters)
        return Page(items=items, total=total, limit=limit, offset=offset)

    async def batch_read(self, session: AsyncSession, ids: list[Any]) -> list[Any]:
        """Return read models in input order, omitting absent ids (per the spec)."""
        await self.authorize(session, "batch_read", ids=ids)
        return await self.repository.get_many_by_ids(session, ids, context=self._ctx())

    async def batch_edit(
        self,
        session: AsyncSession,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        """Apply each edit (id + update payload); return updated read models in order.

        Skips absent ids (idempotent merge semantics per the spec). Edits
        are a list of ``(id, update_model_instance)`` tuples.
        """
        await self.authorize(session, "batch_edit", edits=edits)
        results: list[Any] = []
        for edit_id, payload in edits:
            updated = await self.repository.update_by_id(
                session, edit_id, payload, context=self._ctx()
            )
            if updated is not None:
                results.append(updated)
        return results

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_pagination(self, limit: int, offset: int) -> tuple[int, int]:
        if limit < 1:
            raise InvalidInputError(f"limit must be >= 1, got {limit}")
        if limit > _MAX_LIMIT:
            limit = _MAX_LIMIT
        if offset < 0:
            raise InvalidInputError(f"offset must be >= 0, got {offset}")
        return limit, offset

    def _parse_sort(self, sort: list[str] | None) -> list[tuple[str, bool]] | None:
        """Map ``field`` / ``-field`` tokens to ``(field, ascending)`` tuples.

        Validates each field against the resource's ``sortable`` flag
        (unknown / non-sortable fields -> :class:`InvalidInputError`).
        """
        if not sort:
            return None
        parsed: list[tuple[str, bool]] = []
        for token in sort:
            if token.startswith("-"):
                field_name, ascending = token[1:], False
            else:
                field_name, ascending = token, True
            if field_name not in self.resource.model_fields:
                raise InvalidInputError(f"Unknown sort field {field_name!r}")
            field = self.resource.model_fields[field_name]
            config = self.resource.get_config_for_field(field_name, field)
            if not config.sortable:
                raise InvalidInputError(f"Field {field_name!r} is not sortable")
            parsed.append((field_name, ascending))
        return parsed

    # ------------------------------------------------------------------
    # register — mount the seven actions onto a FastAPI app or router
    # ------------------------------------------------------------------

    def register(
        self,
        app_or_router: FastAPI | APIRouter,
        *,
        prefix: str = "",
        session_dependency: Callable[..., Any] | None = None,
        tags: list[str] | None = None,
    ) -> APIRouter:
        """Build an :class:`APIRouter` with the seven routes and include it.

        Accepts a ``FastAPI`` app, an ``APIRouter``, or any object with
        ``include_router`` (duck-typed). The ``{resource}`` path segment is
        the plural, lower-case, kebab-case name from
        ``resource.get_resource_path()``; action sub-paths use dashes
        (``batch-read``, ``batch-edit``). ``tags`` defaults to
        ``[<RESOURCE_NAME>]`` (the resource class name).

        Each route handler is a thin function: it resolves the
        ``AsyncSession`` from ``session_dependency``, calls the
        corresponding service method, and serialises the response. A route
        is only added if no route already exists at that path + method on
        the target router — a developer who registers a custom route first
        keeps it (escape hatch). Returns the built ``APIRouter``.
        """
        router = APIRouter(tags=tags or [self.resource.__name__])  # type: ignore[arg-type]
        path = "/" + self.resource.get_resource_path().lstrip("/")
        id_type = self._id_python_type()
        session_dep = session_dependency or self._default_session_dependency()

        self._add_create_route(router, path, session_dep)
        # Static sub-paths (batch-read / batch-edit / search) must be registered
        # before the ``{id}`` routes, otherwise ``batch-read`` is captured as an
        # id value by the ``/{resource}/{id}`` route.
        self._add_search_route(router, path, session_dep)
        self._add_batch_read_route(router, path, id_type, session_dep)
        self._add_batch_edit_route(router, path, session_dep)
        self._add_read_route(router, path, id_type, session_dep)
        self._add_update_route(router, path, id_type, session_dep)
        self._add_delete_route(router, path, id_type, session_dep)

        app_or_router.include_router(router, prefix=prefix)
        return router

    # -- route builders ------------------------------------------------
    #
    # Handlers are closures whose parameter types are *local* variables
    # (the create/update models, the id type). With ``from __future__ import
    # annotations`` those annotations would be strings resolved against
    # module globals (where e.g. ``create_model`` is pydantic's function),
    # so each handler's ``__annotations__`` is set explicitly to the real
    # type objects after definition. Defaults (``Depends`` / ``Query``) stay
    # on the signature; only the annotation strings are replaced.

    def _add_create_route(self, router: APIRouter, path: str, session_dep: Any) -> None:
        service = self

        async def handler(payload, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
            result = await service.create(session, payload)
            return _json_response(result, self._ctx(), status.HTTP_201_CREATED)

        handler.__annotations__ = {"payload": self.create_model, "session": AsyncSession}
        self._route(router, path, ["POST"], handler, response_model=None)

    def _add_read_route(self, router: APIRouter, path: str, id_type: Any, session_dep: Any) -> None:
        service = self

        async def handler(id, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.read(session, id)
            return _json_response(result, self._ctx())

        handler.__annotations__ = {"id": id_type, "session": AsyncSession}
        self._route(router, f"{path}/{{id}}", ["GET"], handler, response_model=None)

    def _add_update_route(
        self, router: APIRouter, path: str, id_type: Any, session_dep: Any
    ) -> None:
        service = self

        async def handler(id, payload, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.update(session, id, payload)
            return _json_response(result, self._ctx())

        handler.__annotations__ = {
            "id": id_type,
            "payload": self.update_model,
            "session": AsyncSession,
        }
        self._route(router, f"{path}/{{id}}", ["PATCH"], handler, response_model=None)

    def _add_delete_route(
        self, router: APIRouter, path: str, id_type: Any, session_dep: Any
    ) -> None:
        service = self

        async def handler(id, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            await service.delete(session, id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        handler.__annotations__ = {"id": id_type, "session": AsyncSession}
        self._route(router, f"{path}/{{id}}", ["DELETE"], handler, response_model=None)

    def _add_search_route(self, router: APIRouter, path: str, session_dep: Any) -> None:
        service = self
        filter_cls = self.resource.get_search_filter_type()
        filter_dep = self._filter_dependency(filter_cls) if filter_cls is not None else None
        read_model = self.read_model

        async def handler(  # type: ignore[no-untyped-def]
            request,
            limit=_DEFAULT_LIMIT,
            offset=0,
            sort=None,
            filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
            session=Depends(session_dep),  # noqa: B008
        ):
            sort_tokens = [t.strip() for t in sort.split(",")] if sort else None
            resolved = self._resolve_filters(request, filter_cls, filters)
            page = await service.search(
                session, limit=limit, offset=offset, sort=sort_tokens, filters=resolved
            )
            return _json_response(page, self._ctx())

        handler.__annotations__ = {
            "request": Request,
            "limit": int,
            "offset": int,
            "sort": str | None,
            "session": AsyncSession,
        }
        self._route(
            router,
            path,
            ["GET"],
            handler,
            response_model=Page[read_model],  # type: ignore[valid-type]
        )

    def _add_batch_read_route(
        self, router: APIRouter, path: str, id_type: Any, session_dep: Any
    ) -> None:
        service = self
        batch_path = f"{path}/batch-read"

        async def handler(id=Query(default=[]), session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.batch_read(session, list(id))
            return _json_response(result, self._ctx())

        handler.__annotations__ = {"id": list[id_type], "session": AsyncSession}
        self._route(router, batch_path, ["GET"], handler, response_model=None)

    def _add_batch_edit_route(self, router: APIRouter, path: str, session_dep: Any) -> None:
        service = self
        batch_path = f"{path}/batch-edit"
        item_model = self._batch_edit_item_model()

        async def handler(edits, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
            tuples = [
                (getattr(item, self.id_field), self._item_to_update_model(item)) for item in edits
            ]
            result = await service.batch_edit(session, tuples)
            return _json_response(result, self._ctx())

        handler.__annotations__ = {"edits": list[item_model], "session": AsyncSession}  # type: ignore[valid-type]
        self._route(router, batch_path, ["POST"], handler, response_model=None)

    # -- route escape hatch --------------------------------------------

    def _route(
        self,
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

    # -- HTTP-layer helpers --------------------------------------------

    def _default_session_dependency(self) -> Callable[..., Any]:
        factory = self._session_factory
        if factory is None:
            raise ResourceServiceError(
                "No session dependency configured: pass session_dependency to register() "
                "or session_factory to ResourceService()."
            )

        async def dependency() -> AsyncGenerator[AsyncSession, None]:
            async with factory() as session:
                yield session
                await session.commit()

        return dependency

    def _id_python_type(self) -> Any:
        """The Python type of the id field, for the ``{id}`` path parameter."""
        from resourcey.resource.base import _resolve_scalar_type

        field = self.resource.model_fields[self.id_field]
        return _resolve_scalar_type(field.annotation)

    def _filter_dependency(self, filter_cls: type[SearchFilter[Any]]) -> Callable[..., Any]:
        """Build a FastAPI dependency exposing each declared filter field as a query param.

        The declared ``SearchFilter`` class is the single source of truth for
        what is filterable: its ``model_fields`` (e.g. ``thread_id__eq``,
        ``text__contains``) become individual query parameters in the OpenAPI
        schema, typed from the field annotations. The dependency's signature is
        synthesised from those fields (one ``Query(default=None)`` parameter per
        field) so FastAPI unfolds them into separate OpenAPI parameters and
        coerces/validates each value (422 on a bad type) before the handler runs.

        A synthesised per-field dependency is used rather than
        ``filters: FilterCls = Depends()`` (plain model-as-dependency) for two
        reasons: (1) FastAPI drops list-typed fields from the schema in that
        mode, so the ``in`` operator (``field__in: list[...]``) would silently
        vanish; (2) FastAPI only unfolds a model-typed query param into per-field
        params when it is the *sole* query param (``_get_flat_fields_from_params``
        checks ``len(fields) == 1``) — the search route always has
        ``limit``/``offset``/``sort`` alongside it, so a model param would render
        as a single ``$ref`` instead of separate filter params.

        Returns a callable suitable for ``Depends(...)``; it yields a validated
        ``filter_cls`` instance with only the client-supplied fields set.
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
        self,
        request: StarletteRequest,
        filter_cls: type[SearchFilter[Any]] | None,
        filters: SearchFilter[Any] | None,
    ) -> SearchFilter[Any] | None:
        """Reject unknown ``field__op`` query params; return the validated filter.

        FastAPI collects the declared filter fields into ``filters`` (via the
        generated dependency) and surfaces them in the OpenAPI schema. It does
        not, however, reject *unknown* query parameters — they are silently
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
                f"{self.resource.__name__}; it declares no search filter."
            )
        unknown = filter_params - set(filter_cls.model_fields)
        if unknown:
            raise InvalidInputError(f"Unknown filter parameters {sorted(unknown)}.")
        return filters

    def _batch_edit_item_model(self) -> type[BaseModel]:
        """Build the request-body item model for ``batch-edit``: id + update fields.

        Combines the id field (typed) with the update model's fields so the
        JSON body validates each edit in one pass.
        """
        id_type = self._id_python_type()
        update_fields = {
            name: (field.annotation, field)
            for name, field in self.update_model.model_fields.items()
        }
        model = create_model(  # type: ignore[call-overload]
            f"{self.resource.__name__}BatchEditItem",
            **{self.id_field: (id_type, ...)},  # id is required on each edit
            **update_fields,
        )
        return cast("type[BaseModel]", model)

    def _item_to_update_model(self, item: BaseModel) -> BaseModel:
        """Project a batch-edit item into an update-model instance (drop the id).

        Only fields the client explicitly supplied (not ``MISSING``) are
        carried across, preserving PATCH semantics.
        """
        data = {
            name: getattr(item, name)
            for name in self.update_model.model_fields
            if hasattr(item, name) and getattr(item, name) is not MISSING
        }
        return self.update_model.model_validate(data)


class ResourceServiceError(Exception):
    """The service is misconfigured (e.g. no session dependency supplied)."""


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


def register_error_handlers(app: FastAPI) -> None:
    """Register the consistent error envelope on a FastAPI app.

    Maps framework exceptions to the documented status + code:

    * ``NotFoundError`` -> 404 ``not_found``
    * ``InvalidInputError`` -> 400 ``invalid_input``
    * :class:`sqlalchemy.exc.IntegrityError` -> 409 ``conflict``
    * ``ResourceServiceError`` -> 500 ``internal_error``
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

    @app.exception_handler(ResourceServiceError)
    async def _internal(_: Request, exc: ResourceServiceError) -> JSONResponse:
        return _error_response("internal_error", str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": code, "message": message}},
        status_code=status_code,
    )
