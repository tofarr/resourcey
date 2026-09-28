"""The file-transfer handshake routes (issue #117).

File metadata is a standard DTO-backed resource; the byte *transfer* is a small
handshake of **dedicated routes**, not the eight standard actions, because a
capability URL is computed per request, expiring, and never stored -- it is not
a DTO field, and keeping it off the read model keeps that model static and
cacheable::

    POST {resource}/{id}/upload-url   mint a ``put`` URL
    POST {resource}/{id}/complete     head the object, verify, flip to ready
    GET  {resource}/{id}/download     mint a ``get`` URL (ready files only)
    DELETE {resource}/{id}            the standard delete (also removes the object)

The framework-signed ``put`` / ``get`` transfer endpoints (``PUT`` / ``GET``
``/_files/{key}``) are mounted here too; S3 mints native URLs instead and never
reaches them.

Minting is **authorized**: the handlers resolve the metadata through the
resource's normal :class:`~resourcey.http.dependency_builder.DependencyBuilder`
seam, so a caller must be allowed to act on the file to obtain a URL. The URL is
then a *capability* for its short life, which is the whole point -- an external
object store cannot see the API's auth.

:func:`register_file_routes` is the explicit helper an app calls after
:func:`~resourcey.http.app.create_app`::

    app = create_app(manifest, dependency_builder=builder)
    register_file_routes(app, store, resource=files, dependency_builder=builder)

It adds no :class:`~resourcey.core.service.Action` member and touches no core
code -- a presign handshake is genuinely not one of the eight standard actions.

This module imports no code outside the framework.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from resourcey.core.errors import ConflictError, InvalidInputError
from resourcey.core.resource import Resource
from resourcey.core.service import NotFoundError, Service
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_metadata import READY
from resourcey.filestore.file_store import GET_OPERATION, PUT_OPERATION, FileStore
from resourcey.filestore.signed_url import DEFAULT_SIGNED_URL_PATH, SignedFileStore
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.routes import _dump, _project


def register_file_routes(
    app_or_router: FastAPI | APIRouter,
    store: FileStore,
    *,
    resource: Resource[Any, Any],
    dependency_builder: DependencyBuilder | None = None,
    config: FileStoreConfig | None = None,
    prefix: str = "",
) -> APIRouter:
    """Mount the handshake + framework-signed transfer routes.

    Args:
        app_or_router: A ``FastAPI`` app / ``APIRouter`` (duck-typed).
        store: The medium the bytes move against.
        resource: The file-metadata resource the handshake authorizes through.
        dependency_builder: The seam the minting routes authorize through
            (default :class:`~resourcey.http.dependency_builder.OpenDependencyBuilder`).
        config: TTLs / size cap (default ``FileStoreConfig.get_instance()``).
        prefix: An optional mount prefix.
    """
    exposed = resource.get_exposed_resource() or resource
    resolved_config = config if config is not None else FileStoreConfig.get_instance()
    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()
    service_dep = _service_dependency(exposed, builder)
    dto_type = exposed.get_dto_type()
    read_model = exposed.get_rest_models().read_response
    id_type = _id_type(exposed)
    path = "/" + exposed.get_resource_path().lstrip("/")

    router = APIRouter(tags=[type(exposed).__name__])

    _add_upload_url_route(
        router, path, store, service_dep, id_type, dto_type, read_model, resolved_config
    )
    _add_complete_route(router, path, store, service_dep, id_type, dto_type, read_model)
    _add_download_route(
        router, path, store, service_dep, id_type, dto_type, read_model, resolved_config
    )
    _add_signed_transfer_routes(router, store)

    app_or_router.include_router(router, prefix="" if prefix == "/" else prefix)
    return router


# ---------------------------------------------------------------------------
# Handshake routes
# ---------------------------------------------------------------------------


def _add_upload_url_route(
    router: APIRouter,
    path: str,
    store: FileStore,
    service_dep: Any,
    id_type: Any,
    dto_type: type[Any],
    read_model: type[Any],
    config: FileStoreConfig,
) -> None:
    async def handler(id, request: Request, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008, A002
        record = await service.read(id)
        key = _key_of(record)
        url = store.presign_put(
            key,
            content_type=getattr(record, "content_type", None),
            expires_in_seconds=config.upload_url_ttl_seconds,
        )
        return _url_response(url)

    handler.__annotations__ = {"id": id_type, "request": Request, "service": Service}
    _route(
        router,
        f"{path}/{{id}}/upload-url",
        ["POST"],
        handler,
        summary="Mint an upload URL",
        description="Mint a short-lived capability URL the client PUTs the file bytes against.",
    )


def _add_complete_route(
    router: APIRouter,
    path: str,
    store: FileStore,
    service_dep: Any,
    id_type: Any,
    dto_type: type[Any],
    read_model: type[Any],
) -> None:
    async def handler(id, request: Request, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008, A002
        record = await service.read(id)
        if getattr(record, "status", None) == READY:
            raise ConflictError("File is already complete")
        key = _key_of(record)
        stored = await store.head(key)
        if stored is None:
            raise ConflictError("No object was uploaded for this file")
        _verify_upload(record, stored)
        dto = dto_type(id=id, status=READY, etag=stored.etag)
        updated = await service.update(dto)
        context = service.serialization_context()
        projected = _project(updated, read_model, context)
        return JSONResponse(content=_dump(projected, context))

    handler.__annotations__ = {"id": id_type, "request": Request, "service": Service}
    _route(
        router,
        f"{path}/{{id}}/complete",
        ["POST"],
        handler,
        summary="Complete an upload",
        description="Verify the uploaded object and transition the file to ready.",
    )


def _add_download_route(
    router: APIRouter,
    path: str,
    store: FileStore,
    service_dep: Any,
    id_type: Any,
    dto_type: type[Any],
    read_model: type[Any],
    config: FileStoreConfig,
) -> None:
    async def handler(id, request: Request, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008, A002
        record = await service.read(id)
        if getattr(record, "status", None) != READY:
            raise ConflictError("File is not ready for download")
        key = _key_of(record)
        url = store.presign_get(key, expires_in_seconds=config.download_url_ttl_seconds)
        return _url_response(url)

    handler.__annotations__ = {"id": id_type, "request": Request, "service": Service}
    _route(
        router,
        f"{path}/{{id}}/download",
        ["GET"],
        handler,
        summary="Mint a download URL",
        description="Mint a short-lived capability URL for a ready file's bytes.",
    )


# ---------------------------------------------------------------------------
# Framework-signed transfer routes
# ---------------------------------------------------------------------------


def _add_signed_transfer_routes(router: APIRouter, store: FileStore) -> None:
    """Mount the signed ``PUT`` / ``GET`` transfer endpoints for a signed store.

    Only a :class:`~resourcey.filestore.signed_url.SignedFileStore` serves
    these; an S3 store mints native URLs and a capability never reaches the API.
    """
    if not isinstance(store, SignedFileStore):
        return

    async def put_handler(key: str, request: Request) -> Response:
        signed = _authorize(store, request, key, PUT_OPERATION)
        data = await request.body()
        stored = await signed.put(key, data, content_type=request.headers.get("content-type"))
        return JSONResponse(content=stored.model_dump(mode="json"))

    async def get_handler(key: str, request: Request) -> Response:
        signed = _authorize(store, request, key, GET_OPERATION)
        data = await signed.get(key)
        if data is None:
            raise NotFoundError(key)
        reported = await signed.head(key)
        headers: dict[str, str] = {}
        media_type = "application/octet-stream"
        if reported is not None:
            if reported.content_type:
                media_type = reported.content_type
            if reported.etag:
                headers["ETag"] = reported.etag
        return Response(content=data, media_type=media_type, headers=headers)

    _route(
        router,
        DEFAULT_SIGNED_URL_PATH,
        ["PUT"],
        put_handler,
        summary="Signed object upload",
        include_in_schema=False,
    )
    _route(
        router,
        DEFAULT_SIGNED_URL_PATH,
        ["GET"],
        get_handler,
        summary="Signed object download",
        include_in_schema=False,
    )


def _authorize(
    store: SignedFileStore, request: Request, key: str, operation: str
) -> SignedFileStore:
    """Verify the capability token and that it is bound to the route's key."""
    token = request.query_params.get("token")
    if not token:
        raise InvalidInputError("Missing signed-URL token")
    authorized = store.verify(token, expected_operation=operation)
    if authorized != key:
        raise InvalidInputError("Signed URL is for a different object")
    return store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service_dependency(resource: Resource[Any, Any], builder: DependencyBuilder) -> Any:
    dependency = builder.get_service_dependency(resource)
    if not callable(dependency):
        raise TypeError(
            f"{type(builder).__name__}.get_service_dependency() returned a non-callable "
            f"{dependency!r}; a builder must return a FastAPI dependency."
        )
    return dependency


def _route(
    router: APIRouter,
    path: str,
    methods: list[str],
    handler: Callable[..., Any],
    *,
    summary: str,
    description: str | None = None,
    include_in_schema: bool = True,
) -> None:
    """Add a route unless one already exists at that path + method (escape hatch)."""
    existing = {
        (getattr(route, "path", None), m)
        for route in router.routes
        for m in getattr(route, "methods", set())
    }
    for method in methods:
        if (path, method) in existing:
            continue
        router.add_api_route(
            path,
            handler,
            methods=[method],
            summary=summary,
            description=description,
            include_in_schema=include_in_schema,
            status_code=status.HTTP_200_OK,
        )


def _url_response(url: Any) -> JSONResponse:
    return JSONResponse(
        content={
            "url": url.url,
            "method": url.method,
            "expires_at": url.expires_at.isoformat(),
            "headers": url.headers,
        }
    )


def _key_of(record: Any) -> str:
    key = getattr(record, "key", None)
    if not isinstance(key, str) or not key:
        raise InvalidInputError("File has no storage key")
    return key


def _verify_upload(record: Any, stored: Any) -> None:
    """Verify the uploaded object against the metadata before flipping to ready.

    The object must exist (checked by the caller via ``head``); its size must
    match the row's declared size, and when both the row and the medium record a
    content type they must agree.
    """
    declared_size = getattr(record, "size", None)
    if declared_size is not None and stored.size != declared_size:
        raise ConflictError(f"Uploaded object is {stored.size} bytes, expected {declared_size}")
    declared_type = getattr(record, "content_type", None)
    if (
        declared_type is not None
        and stored.content_type is not None
        and stored.content_type != declared_type
    ):
        raise ConflictError(
            f"Uploaded object has content type {stored.content_type!r}, expected {declared_type!r}"
        )


def _id_type(resource: Resource[Any, Any]) -> Any:
    annotation = (
        resource.get_rest_models().read_response.model_fields[resource.get_id_field()].annotation
    )
    return annotation if isinstance(annotation, type) else str
