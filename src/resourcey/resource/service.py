"""The auto-generated REST service for a resource.

``ResourceService`` is the central deliverable of issue #2. Bound to a
``BaseResource`` subclass, it exposes the standard actions (create, read, update, delete, search, count, batch_read, batch_edit) as
overridable async methods that each take an ``AsyncSession``. It contains
logic (validation orchestration, error mapping, PATCH merge, pagination
assembly, sort validation) and delegates data access to a
:class:`~resourcey.resource.repository.ResourceRepository`.

The service is usable independently of HTTP — call its methods directly with
an ``AsyncSession``. :meth:`ResourceService.register` is a thin convenience
that mounts the standard actions onto a FastAPI app or router.

Auth is explicitly deferred to #4: an overridable :meth:`authorize` hook is
a no-op by default and is called before every action, so a later PR can
fill it in without reworking the action methods or the route wiring.
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from resourcey.cache.cache_header import CacheHeader
from resourcey.resource.cursor import decode_cursor, encode_cursor
from resourcey.resource.errors import InvalidInputError, NotFoundError
from resourcey.resource.missing import MISSING
from resourcey.resource.repository import ResourceRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from resourcey.encryption.encryption_service import EncryptionService
    from resourcey.resource.base import BaseResource
    from resourcey.util.search_filter import SearchFilter


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of cursor-paginated search results.

    ``next_cursor`` is an opaque, encrypted keyset cursor pointing at the last
    row of this page; pass it as the ``cursor`` query param on the next
    request to fetch the following page. It is ``None`` when this page is the
    last (no more rows follow). There is no ``total`` — counting is a separate
    ``count`` action (issue #35).
    """

    items: list[T]
    limit: int
    next_cursor: str | None


# Default pagination bounds. ``limit`` is capped so a client cannot request
# an unbounded scan.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


class ResourceService:
    """The service object exposing the standard resource actions.

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
        cursor: str | None = None,
        sort: str | None = None,
        desc: bool = False,
        filters: SearchFilter[Any] | None = None,
    ) -> Page[Any]:
        """Search with cursor pagination, sort, and optional filters; return a :class:`Page`.

        ``cursor`` is an opaque, encrypted keyset cursor from a previous
        page's ``next_cursor``; ``None`` (or omitted) starts from the first
        page. ``sort`` is a single sortable field name (validated against the
        resource's ``sortable`` flag); ``desc`` selects descending order
        (default ascending). Both are surfaced to :meth:`authorize`. The
        returned ``next_cursor`` is ``None`` when this page is the last.
        """
        await self.authorize(
            session, "search", limit=limit, cursor=cursor, sort=sort, desc=desc, filters=filters
        )
        limit = self._validate_limit(limit)
        sort_parsed = self._parse_sort(sort, desc)
        decoded_cursor = self._decode_cursor(cursor, sort_parsed)
        # Fetch one extra row to detect whether a next page exists without a
        # separate count query (keyset pagination does not use total/offset).
        items = await self.repository.search(
            session,
            limit=limit + 1,
            sort=sort_parsed,
            filters=filters,
            cursor=decoded_cursor,
            context=self._ctx(),
        )
        has_next = len(items) > limit
        items = items[:limit]
        next_cursor = self._next_cursor(items, sort_parsed) if has_next else None
        return Page(items=items, limit=limit, next_cursor=next_cursor)

    async def count(
        self,
        session: AsyncSession,
        *,
        filters: SearchFilter[Any] | None = None,
    ) -> int:
        """Return the number of rows matching ``filters`` (decoupled from paging/sort).

        Reuses the ``"search"`` permission — counting is not a separate
        privilege from listing. Delegates to the repository's existing
        ``count()`` (``select(func.count())`` with the same ``filters``).
        """
        await self.authorize(session, "search", filters=filters)
        return await self.repository.count(session, filters=filters)

    async def batch_read(self, session: AsyncSession, ids: list[Any]) -> list[Any]:
        """Return read models positionally aligned with the input ids.

        Each position ``i`` holds the read model for ``ids[i]`` or ``None`` if
        no such entity exists, so the response length always equals the input
        length and callers can correlate results by index.
        """
        await self.authorize(session, "batch_read", ids=ids)
        return await self.repository.get_many_by_ids(session, ids, context=self._ctx())

    async def batch_edit(
        self,
        session: AsyncSession,
        edits: list[tuple[Any, BaseModel]],
    ) -> list[Any]:
        """Apply each edit (id + update payload); return results in input order.

        Maintains a 1:1 positional correspondence with the input edits: each
        position ``i`` holds the updated read model for ``edits[i]`` or
        ``None`` if that id does not exist (no DB write). The merge is
        idempotent — re-applying the same batch yields the same results and
        leaves absent ids untouched. Edits are a list of
        ``(id, update_model_instance)`` tuples.
        """
        await self.authorize(session, "batch_edit", edits=edits)
        results: list[Any] = []
        for edit_id, payload in edits:
            updated = await self.repository.update_by_id(
                session, edit_id, payload, context=self._ctx()
            )
            results.append(updated)
        return results

    # ------------------------------------------------------------------
    # Cache header computation (logic; delegates to the resource strategy)
    # ------------------------------------------------------------------

    def compute_cache_header(self, items: list[Any]) -> CacheHeader | None:
        """Resolve the resource's cache strategy and compute a header for ``items``.

        Returns the :class:`~resourcey.cache.cache_header.CacheHeader`, or
        ``None`` when the strategy yields no validators and no expiry (so the
        HTTP layer skips header setting entirely). The serialization context
        (``self._ctx()``) is threaded into the strategy so the ETag validates
        the same bytes the response body serializes to.
        """
        header = self.resource.get_cache_strategy().get_cache_header(items, context=self._ctx())
        return header if header.has_any() else None

    def compute_count_cache_header(
        self,
        count: int,
        filters: SearchFilter[Any] | None,
    ) -> CacheHeader | None:
        """Compute a count-derived cache header for the ``count`` route.

        ``count`` returns a bare integer, not read-model instances, so the
        model-based ``get_cache_header`` does not apply. The ETag is a stable
        hash of the count value together with the canonical-JSON serialization
        of the resolved filter (distinct filters get distinct ETags). No
        ``Last-Modified`` (a row delete changes the count without touching any
        ``updated_at``, so last-modified is an unreliable validator for a
        count). ``expire_in`` from the resource's strategy is honoured.
        """
        from resourcey.cache.cache_strategy import _digest, _stable_json

        strategy = self.resource.get_cache_strategy()
        parts: list[bytes] = [str(count).encode("utf-8"), b"\n"]
        if filters is not None:
            parts.append(_stable_json(filters.model_dump(mode="json")).encode("utf-8"))
        header = CacheHeader(etag=f'"{_digest(parts)}"')
        return strategy.with_expiry(header) if header.has_any() else None

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_limit(self, limit: int) -> int:
        if limit < 1:
            raise InvalidInputError(f"limit must be >= 1, got {limit}")
        if limit > _MAX_LIMIT:
            limit = _MAX_LIMIT
        return limit

    def _encryption_service(self) -> EncryptionService:
        """The encryption service used to encrypt/decrypt cursors.

        Sourced from the serialization context (shared with at-rest field
        encryption) when present, otherwise the process-wide singleton. The
        singleton is always available in production (the encryption key is
        required config); tests set ``RESOURCEY_ENCRYPTION_KEY_*`` env vars.
        """
        from resourcey.encryption.encryption_service import get_encryption_service

        ctx = self._serialization_context
        if ctx is not None:
            enc = ctx.get("encryption_service")
            if enc is not None:
                return enc  # type: ignore[no-any-return]
        return get_encryption_service()

    def _sort_key_field(self, sort_parsed: tuple[str, bool] | None) -> str:
        """The field whose value the cursor keys off (id when no sort is requested)."""
        if sort_parsed is None:
            return self.id_field
        return sort_parsed[0]

    def _decode_cursor(
        self,
        cursor: str | None,
        sort_parsed: tuple[str, bool] | None,
    ) -> tuple[Any, Any] | None:
        """Decrypt an opaque cursor into a ``(sort_key, id)`` pair, or ``None``.

        Validates that the cursor was built for the same ``(sort_field,
        ascending)`` as the current request: a cursor from a ``sort=size``
        page reused under ``sort=created_at`` (or no sort) would apply the
        decrypted key against the wrong column, yielding silently wrong
        results, so it is rejected with ``400 invalid_input``.
        """
        if not cursor:
            return None
        try:
            c_field, c_ascending, sort_key, id_value = decode_cursor(
                self._encryption_service(), cursor
            )
        except (ValueError, KeyError) as exc:
            raise InvalidInputError(f"Invalid or tampered cursor: {exc}") from exc
        expected_field = sort_parsed[0] if sort_parsed is not None else None
        expected_ascending = sort_parsed[1] if sort_parsed is not None else True
        if c_field != expected_field or c_ascending != expected_ascending:
            raise InvalidInputError(
                "Cursor was built for a different sort than the current request; "
                "start a new search without a cursor when changing sort."
            )
        return sort_key, id_value

    def _next_cursor(
        self,
        items: list[Any],
        sort_parsed: tuple[str, bool] | None,
    ) -> str | None:
        """Encode a ``next_cursor`` from the last item, or ``None`` if the page is exhausted."""
        if not items:
            return None
        last = items[-1]
        field = self._sort_key_field(sort_parsed)
        sort_key = getattr(last, field)
        id_value = getattr(last, self.id_field)
        sort_field = sort_parsed[0] if sort_parsed is not None else None
        ascending = sort_parsed[1] if sort_parsed is not None else True
        return encode_cursor(
            self._encryption_service(),
            sort_field=sort_field,
            ascending=ascending,
            sort_key=sort_key,
            id_value=id_value,
        )

    def _parse_sort(self, sort: str | None, desc: bool) -> tuple[str, bool] | None:
        """Map a sort field name + ``desc`` flag to a ``(field, ascending)`` tuple.

        Validates the field against the resource's ``sortable`` flag (unknown
        / non-sortable fields -> :class:`InvalidInputError`). Returns ``None``
        when no sort is requested. ``ascending`` is ``not desc``.
        """
        if not sort:
            return None
        if sort not in self.resource.model_fields:
            raise InvalidInputError(f"Unknown sort field {sort!r}")
        field = self.resource.model_fields[sort]
        config = self.resource.get_config_for_field(sort, field)
        if not config.sortable:
            raise InvalidInputError(f"Field {sort!r} is not sortable")
        return sort, not desc

    # ------------------------------------------------------------------
    # register — mount the standard actions onto a FastAPI app or router
    # ------------------------------------------------------------------

    def register(
        self,
        app_or_router: FastAPI | APIRouter,
        *,
        prefix: str = "",
        session_dependency: Callable[..., Any] | None = None,
        tags: list[str] | None = None,
    ) -> APIRouter:
        """Build an :class:`APIRouter` with the standard routes and include it.

        Accepts a ``FastAPI`` app, an ``APIRouter``, or any object with
        ``include_router`` (duck-typed). The ``{resource}`` path segment is
        the plural, lower-case, kebab-case name from
        ``resource.get_resource_path()``; action sub-paths use dashes
        (``batch-read``, ``batch-edit``, ``count``). ``tags`` defaults to
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
        # Static sub-paths (search / count / batch-read / batch-edit) must be
        # registered before the ``{id}`` routes, otherwise ``batch-read`` is
        # captured as an id value by the ``/{resource}/{id}`` route.
        self._add_search_route(router, path, session_dep)
        self._add_count_route(router, path, session_dep)
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

        async def handler(request, payload, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
            result = await service.create(session, payload)
            header = service.compute_cache_header([result])
            return _cached_json_response(
                request, result, self._ctx(), header, status.HTTP_201_CREATED
            )

        handler.__annotations__ = {
            "request": Request,
            "payload": self.create_model,
            "session": AsyncSession,
        }
        self._route(router, path, ["POST"], handler, response_model=None)

    def _add_read_route(self, router: APIRouter, path: str, id_type: Any, session_dep: Any) -> None:
        service = self

        async def handler(request, id, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.read(session, id)
            header = service.compute_cache_header([result])
            return _cached_json_response(request, result, self._ctx(), header)

        handler.__annotations__ = {"request": Request, "id": id_type, "session": AsyncSession}
        self._route(router, f"{path}/{{id}}", ["GET"], handler, response_model=None)

    def _add_update_route(
        self, router: APIRouter, path: str, id_type: Any, session_dep: Any
    ) -> None:
        service = self

        async def handler(request, id, payload, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.update(session, id, payload)
            header = service.compute_cache_header([result])
            return _cached_json_response(request, result, self._ctx(), header)

        handler.__annotations__ = {
            "request": Request,
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
        sortable = self.resource.get_sortable_fields()
        read_model = self.read_model

        if sortable:
            handler = self._sortable_search_handler(
                service, filter_cls, filter_dep, sortable, session_dep
            )
        else:
            handler = self._sortless_search_handler(service, filter_cls, filter_dep, session_dep)
        self._route(
            router,
            path,
            ["GET"],
            handler,
            response_model=Page[read_model],  # type: ignore[valid-type]
        )

    def _sortable_search_handler(
        self,
        service: ResourceService,
        filter_cls: type[SearchFilter[Any]] | None,
        filter_dep: Callable[..., Any] | None,
        sortable: list[str],
        session_dep: Any,
    ) -> Callable[..., Any]:
        """Build the GET search handler for a resource with sortable fields.

        ``sort`` is an enum of the resource's sortable fields, so FastAPI
        validates it (422 on an unknown / injected value) before the handler
        runs. ``desc`` selects direction (default ascending). ``cursor`` is
        an opaque keyset cursor from a previous page's ``next_cursor``.
        """
        # A dynamic StrEnum (member names are the field names) gives OpenAPI a
        # concrete enum and FastAPI request validation that rejects unknown /
        # injected sort values.
        sort_enum = enum.StrEnum(  # type: ignore[misc]
            f"{self.resource.__name__}SortField", {n: n for n in sortable}
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
            session=Depends(session_dep),  # noqa: B008
        ):
            resolved = self._resolve_filters(request, filter_cls, filters)
            page = await service.search(
                session,
                limit=limit,
                cursor=cursor,
                sort=sort.value if sort is not None else None,
                desc=desc,
                filters=resolved,
            )
            header = service.compute_cache_header(page.items)
            return _cached_json_response(request, page, self._ctx(), header)

        handler.__annotations__ = {
            "request": Request,
            "limit": int,
            "cursor": str | None,
            "sort": sort_enum | None,
            "desc": bool,
            "session": AsyncSession,
        }
        return handler

    def _sortless_search_handler(
        self,
        service: ResourceService,
        filter_cls: type[SearchFilter[Any]] | None,
        filter_dep: Callable[..., Any] | None,
        session_dep: Any,
    ) -> Callable[..., Any]:
        """Build the GET search handler for a resource with no sortable fields.

        ``sort`` / ``desc`` are not exposed. A residual check preserves the
        contract that ``?sort=`` on a sortless resource is rejected (400)
        rather than silently ignored. ``cursor`` is an opaque keyset cursor
        from a previous page's ``next_cursor``.
        """
        cursor_default: Any = Query(default=None)

        async def handler(  # type: ignore[no-untyped-def]
            request,
            limit=_DEFAULT_LIMIT,
            cursor=cursor_default,
            filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
            session=Depends(session_dep),  # noqa: B008
        ):
            if "sort" in request.query_params or "desc" in request.query_params:
                raise InvalidInputError(
                    f"Sort parameters are not supported on {self.resource.__name__}; "
                    f"it declares no sortable fields."
                )
            resolved = self._resolve_filters(request, filter_cls, filters)
            page = await service.search(session, limit=limit, cursor=cursor, filters=resolved)
            header = service.compute_cache_header(page.items)
            return _cached_json_response(request, page, self._ctx(), header)

        handler.__annotations__ = {
            "request": Request,
            "limit": int,
            "cursor": str | None,
            "session": AsyncSession,
        }
        return handler

    def _add_count_route(self, router: APIRouter, path: str, session_dep: Any) -> None:
        """Register ``GET /{resource}/count`` — matching row count for a filter.

        Accepts the same ``field__op=value`` filter query params as ``search``
        (filter validation is shared via :meth:`_resolve_filters`), but no
        ``sort`` / ``limit`` / ``cursor`` (ordering and paging are meaningless
        for a count). Returns a bare integer. Permission reuses ``"search"``.
        """
        service = self
        filter_cls = self.resource.get_search_filter_type()
        filter_dep = self._filter_dependency(filter_cls) if filter_cls is not None else None
        count_path = f"{path}/count"

        async def handler(  # type: ignore[no-untyped-def]
            request,
            filters=Depends(filter_dep) if filter_dep is not None else None,  # noqa: B008
            session=Depends(session_dep),  # noqa: B008
        ):
            if {"sort", "desc", "limit", "cursor"} & set(request.query_params):
                raise InvalidInputError(
                    "count accepts only filter parameters; sort/limit/cursor are not allowed."
                )
            resolved = self._resolve_filters(request, filter_cls, filters)
            total = await service.count(session, filters=resolved)
            header = service.compute_count_cache_header(total, resolved)
            return _cached_json_response(request, total, None, header)

        handler.__annotations__ = {"request": Request, "session": AsyncSession}
        self._route(router, count_path, ["GET"], handler, response_model=None)

    def _add_batch_read_route(
        self, router: APIRouter, path: str, id_type: Any, session_dep: Any
    ) -> None:
        service = self
        batch_path = f"{path}/batch-read"

        async def handler(request, id=Query(default=[]), session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
            result = await service.batch_read(session, list(id))
            header = service.compute_cache_header(result)
            return _cached_json_response(request, result, self._ctx(), header)

        handler.__annotations__ = {"request": Request, "id": list[id_type], "session": AsyncSession}
        self._route(router, batch_path, ["GET"], handler, response_model=None)

    def _add_batch_edit_route(self, router: APIRouter, path: str, session_dep: Any) -> None:
        service = self
        batch_path = f"{path}/batch-edit"
        item_model = self._batch_edit_item_model()

        async def handler(request, edits, session=Depends(session_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
            tuples = [
                (getattr(item, self.id_field), self._item_to_update_model(item)) for item in edits
            ]
            result = await service.batch_edit(session, tuples)
            header = service.compute_cache_header(result)
            return _cached_json_response(request, result, self._ctx(), header)

        handler.__annotations__ = {
            "request": Request,
            "edits": list[item_model],  # type: ignore[valid-type]
            "session": AsyncSession,
        }
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
        ``limit``/``cursor``/``sort`` alongside it, so a model param would render
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
    from email.utils import parsedate_to_datetime

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
    the request is a safe method (``GET`` / ``HEAD`` — RFC 7232 restricts
    ``304 Not Modified`` to safe methods) and the client's conditional request
    headers prove the copy is current (``header.is_modified(client)`` is
    ``False``) a ``304 Not Modified`` with an empty body (but the validator +
    ``Cache-Control`` headers) is returned. Unsafe methods (``POST`` / ``PATCH``
    / ``DELETE``) still emit the headers on the response but always send the
    body — they cannot short-circuit to ``304``.
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
