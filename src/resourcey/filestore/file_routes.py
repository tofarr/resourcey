"""The ``files`` surface's hand-written routes (issue #117, #158).

``files`` is an ordinary resource (:mod:`resourcey.filestore.file_resource`),
so :func:`~resourcey.http.routes.register_routes` already mounts its standard
surface -- ``read`` / ``delete`` / ``search`` / ``count`` / ``batch_read`` /
``batch_edit`` -- correctly. Three things still need hand-written routes:

* ``create`` -- not among the actions ``register_routes`` generates at all
  (``files`` does not declare ``Action.CREATE``; see
  :mod:`~resourcey.filestore.file_resource`'s docstring for why). It is a
  direct ``multipart/form-data`` upload (case 2 of the upload-design
  discussion), answering the ordinary ``201 Created`` once the bytes have
  genuinely landed -- there is no capability-minting step to make a placeholder
  status code necessary.
* ``download`` (the JSON capability, unchanged from #117) and ``content``
  (bytes, #158) are not among the eight standard actions at all.

::

    POST   {resource}                 the upload itself (201; multipart/form-data)
    GET    {resource}/{id}/download   mint a ``get`` capability (the JSON shape)
    GET    {resource}/{id}/content    the bytes: a redirect for S3, streamed
                                       directly for Local / SQL

The framework-signed ``GET`` ``/_files/{key}`` transfer endpoint is mounted
here too, for a :class:`~resourcey.filestore.signed_url.SignedFileStore`
medium (Local / SQL); S3 mints native URLs and never reaches it. Upload has no
transfer endpoint of its own to mount -- ``create`` *is* the upload, served
directly by the route above.

:func:`register_file_routes` is called **after**
:func:`~resourcey.http.app.create_app`, exactly like every other after-the-fact
mount in this framework (``register_oauth_routes``, the earlier
``register_file_routes``), and is the **sole** place ``files``'s routes are
mounted -- it calls ``register_routes`` itself (which, with no ``Action.CREATE``
declared, mounts every *other* standard route correctly and nothing for
``create``), so the resource must **not** also be listed in the
``Manifest(resources=...)`` passed to ``create_app`` (that would register its
other standard routes a second time, on its own router). The medium still goes
in the manifest's ``managers`` slot exactly as before::

    manifest = Manifest(resources=[], managers=[store])  # files is not a manifest resource
    app = create_app(manifest, dependency_builder=builder)
    register_file_routes(app, store, resource=files, dependency_builder=builder)

This module imports no code outside the framework.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse

from resourcey.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import NotFoundError
from resourcey.filestore.file_config import FileStoreConfig
from resourcey.filestore.file_store import FileStore
from resourcey.filestore.signed_url import DEFAULT_SIGNED_URL_PATH, SignedFileStore
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.routes import (
    _cached_json_response,
    _dump,
    _header_for,
    _project,
    _resource_display_name,
    _service_dependency,
    register_routes,
)

# Bounds how much of an over-cap upload this process buffers before rejecting
# it: the upload is read and hashed in chunks, so a ``max_size`` cap is
# enforced as soon as it is exceeded rather than only after the whole body
# has already been read into memory.
_UPLOAD_CHUNK_SIZE = 1024 * 1024


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
        config: TTL / size cap (default ``FileStoreConfig.get_instance()``);
            a ``max_size`` the resource itself was constructed with takes
            precedence over this config's, mirroring the resource-level
            override :class:`~resourcey.filestore.file_resource.FileResource`
            already honours for the service-level enforcement.
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
    max_size = resource.max_size if hasattr(resource, "max_size") else resolved_config.max_size

    router = register_routes(
        app_or_router, resource, prefix=prefix, dependency_builder=dependency_builder
    )
    path = "/" + exposed.get_resource_path().lstrip("/")

    _add_create_route(router, exposed, builder, path, max_size)
    _add_download_route(router, exposed, store, builder, path, resolved_config)
    _add_content_route(router, exposed, store, builder, path, resolved_config)
    _add_signed_transfer_routes(router, store)
    return router


# ---------------------------------------------------------------------------
# create -- the upload itself
# ---------------------------------------------------------------------------


def _add_create_route(
    router: APIRouter,
    exposed: Resource[Any, Any],
    builder: DependencyBuilder,
    path: str,
    max_size: int | None,
) -> None:
    """Mount ``POST {path}``: a ``multipart/form-data`` body with one ``file`` part.

    This is the same shape a plain HTML ``<input type="file">`` form already
    produces, so a no-JS form -- and Swagger UI's "Try it out" -- can create a
    file directly, with no separate capability step. ``name`` / ``content_type``
    are read off the upload; the service computes ``size`` / ``checksum`` from
    the bytes it actually receives. Not among the actions
    ``register_routes`` mounts (``files`` does not declare ``Action.CREATE``),
    so there is no generated route to replace here, only one to add.
    """
    service_dep = _service_dependency(exposed, builder)
    models = exposed.get_rest_models()
    dto_model = exposed.get_dto_type()
    resource_name = _resource_display_name(exposed)
    strategy = exposed.get_cache_strategy()
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None

    async def handler(  # type: ignore[no-untyped-def]
        request: Request,
        file: UploadFile = File(...),  # noqa: B008
        service=Depends(service_dep),  # noqa: B008
    ):
        name = file.filename
        if not name:
            raise InvalidInputError("The uploaded file has no filename")
        content_type = file.content_type or "application/octet-stream"
        chunks: list[bytes] = []
        total = 0
        while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
            total += len(chunk)
            if max_size is not None and total > max_size:
                raise InvalidInputError(f"Upload exceeds the {max_size}-byte cap")
            chunks.append(chunk)
        payload = dto_model(name=name, content_type=content_type, content=b"".join(chunks))
        created = await service.create(payload)
        context = service.serialization_context()
        projected = _project(created, models.create_response, context)
        header = _header_for(strategy, [projected], context, service)
        return _cached_json_response(
            request, _dump(projected, context), header, status.HTTP_201_CREATED
        )

    router.add_api_route(
        path,
        handler,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
        dependencies=route_deps,
        response_model=None,
        summary=f"Create {resource_name}",
        description=(
            f"Upload a new {resource_name}: a multipart/form-data body with one `file` part. "
            "`name` / `content_type` are read from the upload; `size` / `checksum` are computed "
            "from the bytes received, not a client declaration."
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
    """Mount the signed ``GET`` transfer endpoint for a signed store.

    Only a :class:`~resourcey.filestore.signed_url.SignedFileStore` serves
    this (Local / SQL); an S3 store mints native URLs and a capability never
    reaches the API. There is no ``PUT`` counterpart: upload has no capability
    of its own to serve -- ``create`` *is* the upload, mounted directly by
    :func:`_add_create_route` above.
    """
    if not isinstance(store, SignedFileStore):
        return

    async def get_handler(key: str, request: Request) -> Response:
        _verify(store, request, key)
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


def _verify(store: SignedFileStore, request: Request, key: str) -> None:
    """Verify the capability token and that it is bound to the route's key."""
    token = request.query_params.get("token")
    if not token:
        raise InvalidInputError("Missing signed-URL token")
    verified_key = store.verify(token)
    if verified_key != key:
        raise InvalidInputError("Signed URL is for a different object")


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
