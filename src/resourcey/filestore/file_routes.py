"""The file-transfer routes (issue #117, #158).

``files`` is an ordinary resource (:mod:`resourcey.filestore.file_resource`),
so :func:`~resourcey.http.routes.register_routes` already mounts its standard
surface -- ``read`` / ``delete`` / ``search`` / ``count`` / ``batch_read`` /
``batch_edit`` -- correctly. Two things still need hand-written routes:

* ``create`` must mint the upload capability and answer **``202 Accepted``**
  (nothing exists yet), not the generated builder's hardcoded ``201``. The
  generated route is still what makes ``batch_edit``'s ``Create`` kind and the
  create defaults resolve correctly (both read the one
  ``get_supported_actions()`` declaration), so :func:`register_file_routes`
  lets ``register_routes`` build everything first and then **swaps** the one
  generated ``POST {resource}`` route for a hand-written one -- the same
  "a developer's own route wins" escape hatch, applied after the fact rather
  than before it, so there is exactly one route for the path in both the
  runtime dispatch *and* the OpenAPI schema (no shadowed duplicate operation).
* ``download`` (the JSON capability, unchanged from #117) and the new
  ``content`` (bytes) routes are not among the eight standard actions at all.

::

    POST   {resource}            mint an upload capability (202)
    GET    {resource}/{id}/download   mint a ``get`` capability (the JSON shape)
    GET    {resource}/{id}/content    the bytes: a redirect for S3, streamed
                                       directly for Local / SQL

The framework-signed ``PUT`` / ``GET`` ``/_files/{key}`` transfer endpoints are
mounted here too, for a :class:`~resourcey.filestore.signed_url.SignedFileStore`
medium (Local / SQL); S3 mints native URLs and never reaches them. The ``PUT``
handler verifies the uploaded bytes against the capability's signed claims
(:func:`~resourcey.filestore.file_store.verify_upload`, via the medium's
``put``) before they become visible.

:func:`register_file_routes` is called **after**
:func:`~resourcey.http.app.create_app`, exactly like every other after-the-fact
mount in this framework (``register_oauth_routes``, the earlier
``register_file_routes``), and is the **sole** place ``files``'s routes are
mounted -- it calls ``register_routes`` itself (to get the swap-then-replace
right), so the resource must **not** also be listed in the
``Manifest(resources=...)`` passed to ``create_app`` (that would register the
generated ``201`` route a second time, on its own router, which runs first and
shadows the swap below). The medium still goes in the manifest's ``managers``
slot exactly as before::

    manifest = Manifest(resources=[], managers=[store])  # files is not a manifest resource
    app = create_app(manifest, dependency_builder=builder)
    register_file_routes(app, store, resource=files, dependency_builder=builder)

This module imports no code outside the framework.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from resourcey.core.dto import request_to_dto
from resourcey.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import NotFoundError, Service
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_store import GET_OPERATION, PUT_OPERATION, FileStore
from resourcey.filestore.signed_url import DEFAULT_SIGNED_URL_PATH, SignedFileStore
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.routes import (
    _dump,
    _project,
    _resource_display_name,
    _service_dependency,
    register_routes,
)


def register_file_routes(
    app_or_router: FastAPI | APIRouter,
    store: FileStore,
    *,
    resource: Resource[Any, Any],
    dependency_builder: DependencyBuilder | None = None,
    config: FileStoreConfig | None = None,
    prefix: str = "",
) -> APIRouter:
    """Mount the ``files`` surface: the standard actions plus the hand-written ones.

    Args:
        app_or_router: A ``FastAPI`` app / ``APIRouter`` (duck-typed).
        store: The medium the bytes move against.
        resource: The ``files`` resource (see :mod:`~resourcey.filestore.file_resource`).
        dependency_builder: The seam every route authorizes through (default
            :class:`~resourcey.http.dependency_builder.OpenDependencyBuilder`).
        config: TTLs / size cap (default ``FileStoreConfig.get_instance()``).
        prefix: An optional mount prefix.
    """
    if resource.get_manifest() is not None:
        raise ResourceyConfigError(
            "The files resource is already registered on a Manifest; register_file_routes "
            "mounts its routes itself and must be the only caller of register_routes for it "
            "-- pass resources=[] (or omit it) for this resource and keep only its medium in "
            "managers=[...]."
        )
    exposed = resource.get_exposed_resource() or resource
    resolved_config = config if config is not None else FileStoreConfig.get_instance()
    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()

    router = register_routes(
        app_or_router, resource, prefix=prefix, dependency_builder=dependency_builder
    )
    path = "/" + exposed.get_resource_path().lstrip("/")

    _replace_create_route(router, exposed, builder, path)
    _add_download_route(router, exposed, store, builder, path, resolved_config)
    _add_content_route(router, exposed, store, builder, path, resolved_config)
    _add_signed_transfer_routes(router, store)
    return router


# ---------------------------------------------------------------------------
# create -- swap the generated 201 route for a hand-written 202 one
# ---------------------------------------------------------------------------


def _replace_create_route(
    router: APIRouter,
    exposed: Resource[Any, Any],
    builder: DependencyBuilder,
    path: str,
) -> None:
    """Drop the generated ``POST {path}`` route and mount our own (202).

    ``register_routes`` already built it (from the same ``get_supported_actions()``
    declaration ``batch_edit``'s ``Create`` kind needs), so it is removed --
    not left to be shadowed -- so exactly one route exists for the path, in
    both the live dispatch *and* the generated OpenAPI schema.
    """
    router.routes = [
        r
        for r in router.routes
        if not (getattr(r, "path", None) == path and "POST" in (getattr(r, "methods", None) or ()))
    ]
    service_dep = _service_dependency(exposed, builder)
    models = exposed.get_rest_models()
    dto_model = exposed.get_dto_type()
    resource_name = _resource_display_name(exposed)
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None

    async def handler(payload, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: B008
        created = await service.create(request_to_dto(dto_model, payload))
        context = service.serialization_context()
        projected = _project(created, models.create_response, context)
        return JSONResponse(content=_dump(projected, context), status_code=status.HTTP_202_ACCEPTED)

    handler.__annotations__ = {"payload": models.create_request, "service": Service}
    router.add_api_route(
        path,
        handler,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=route_deps,
        response_model=None,
        summary=f"Create {resource_name}",
        description=(
            f"Allocate a storage key and mint an upload capability for a new {resource_name}. "
            "Nothing is persisted until the upload lands."
        ),
    )


# ---------------------------------------------------------------------------
# download / content
# ---------------------------------------------------------------------------


def _add_download_route(
    router: APIRouter,
    exposed: Resource[Any, Any],
    store: FileStore,
    builder: DependencyBuilder,
    path: str,
    config: FileStoreConfig,
) -> None:
    """``GET {path}/{id}/download`` -- the JSON capability, unchanged from #117."""
    service_dep = _service_dependency(exposed, builder)
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None

    async def handler(id: str, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        await service.read(id)
        url = store.presign_get(id, expires_in_seconds=config.download_url_ttl_seconds)
        return _url_response(url)

    router.add_api_route(
        f"{path}/{{id}}/download",
        handler,
        methods=["GET"],
        dependencies=route_deps,
        response_model=None,
        summary="Mint a download URL",
        description="Mint a short-lived capability URL for this file's bytes.",
    )


def _add_content_route(
    router: APIRouter,
    exposed: Resource[Any, Any],
    store: FileStore,
    builder: DependencyBuilder,
    path: str,
    config: FileStoreConfig,
) -> None:
    """``GET {path}/{id}/content`` -- the bytes.

    A ``307`` redirect to a fresh presigned ``GET`` for S3 (the only sane
    option for a non-browser client -- a native presigned URL is the thing
    that tells it when the capability expires); streamed directly for Local /
    SQL, where a redirect to ``/_files/{key}`` would buy nothing since the
    request never leaves this process anyway.
    """
    service_dep = _service_dependency(exposed, builder)
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None
    streams_directly = isinstance(store, SignedFileStore)

    async def handler(id: str, service=Depends(service_dep)):  # type: ignore[no-untyped-def]  # noqa: A002, B008
        found = await service.read(id)
        if streams_directly:
            data = await store.get(id)
            if data is None:
                raise NotFoundError(id)
            media_type = getattr(found, "content_type", None) or "application/octet-stream"
            headers = {}
            etag = getattr(found, "etag", None)
            if etag:
                headers["ETag"] = etag
            return Response(content=data, media_type=media_type, headers=headers)
        url = store.presign_get(id, expires_in_seconds=config.download_url_ttl_seconds)
        return RedirectResponse(url=url.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    router.add_api_route(
        f"{path}/{{id}}/content",
        handler,
        methods=["GET"],
        dependencies=route_deps,
        response_model=None,
        summary="Fetch this file's bytes",
        description=(
            "Redirects to a presigned URL (S3) or streams the bytes directly (Local / SQL). "
            "A plain `<a href>` / `<img src>` cannot carry an `Authorization` header, so this "
            "route is only ergonomic unauthenticated or behind a cookie authenticator."
        ),
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
        capability = _verify(store, request, key, PUT_OPERATION)
        data = await request.body()
        stored = await store.put(
            key,
            data,
            content_type=capability.content_type,
            name=capability.name,
            checksum=capability.checksum,
            declared_size=capability.size,
        )
        return JSONResponse(content=stored.model_dump(mode="json"))

    async def get_handler(key: str, request: Request) -> Response:
        _verify(store, request, key, GET_OPERATION)
        data = await store.get(key)
        if data is None:
            raise NotFoundError(key)
        reported = await store.head(key)
        headers: dict[str, str] = {}
        media_type = "application/octet-stream"
        if reported is not None:
            if reported.content_type:
                media_type = reported.content_type
            if reported.etag:
                headers["ETag"] = reported.etag
        return Response(content=data, media_type=media_type, headers=headers)

    if not _existing_route(router, DEFAULT_SIGNED_URL_PATH, "PUT"):
        router.add_api_route(
            DEFAULT_SIGNED_URL_PATH,
            put_handler,
            methods=["PUT"],
            response_model=None,
            summary="Signed object upload",
            include_in_schema=False,
        )
    if not _existing_route(router, DEFAULT_SIGNED_URL_PATH, "GET"):
        router.add_api_route(
            DEFAULT_SIGNED_URL_PATH,
            get_handler,
            methods=["GET"],
            response_model=None,
            summary="Signed object download",
            include_in_schema=False,
        )


def _existing_route(router: APIRouter, path: str, method: str) -> bool:
    return any(
        getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ())
        for r in router.routes
    )


def _verify(store: SignedFileStore, request: Request, key: str, operation: str) -> Any:
    """Verify the capability token and that it is bound to the route's key."""
    token = request.query_params.get("token")
    if not token:
        raise InvalidInputError("Missing signed-URL token")
    capability = store.verify(token, expected_operation=operation)
    if capability.key != key:
        raise InvalidInputError("Signed URL is for a different object")
    return capability


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_response(url: Any) -> JSONResponse:
    return JSONResponse(
        content={
            "url": url.url,
            "method": url.method,
            "expires_at": url.expires_at.isoformat(),
            "headers": url.headers,
        }
    )
