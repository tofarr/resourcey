"""App assembly: :func:`create_app` and :func:`add_to_app`.

These are **free functions**, not methods on
:class:`~resourcey.core.manifest.Manifest`, so HTTP construction stays out of
``core``. ``create_app`` builds a fresh :class:`~fastapi.FastAPI` wired to the
manifest's lifespan, its routes, error handlers, and optional CORS.
``add_to_app`` mounts the same onto a user-owned app and deliberately does
**not** wire a lifespan — Starlette has a single lifespan slot, so the caller
composes it explicitly.

This module imports no code outside the framework.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from resourcey.core.manifest import Manifest
from resourcey.http.dependency_builder import DependencyBuilder
from resourcey.http.routes import register_error_handlers, register_routes


def create_app(
    manifest: Manifest,
    *,
    cors_origins: list[str] | None = None,
    dependency_builder: DependencyBuilder | None = None,
    tags: Sequence[str] | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app wired to this manifest's lifecycle.

    For a custom lifespan or a pre-existing app, use :func:`add_to_app` and
    ``async with manifest`` manually.

    Args:
        manifest: The resource set to serve.
        cors_origins: Allowed CORS origins; empty/``None`` adds no middleware.
        dependency_builder: How each resource's per-request service dependency
            is built; ``None`` uses the default builder.
        tags: OpenAPI tags override applied to **every** resource's routes.
            Rarely wanted — it puts every resource in one group.
    """
    app = FastAPI(lifespan=_lifespan(manifest))
    _configure_cors(app, cors_origins)
    add_to_app(manifest, app, dependency_builder=dependency_builder, tags=tags)
    return app


def add_to_app(
    manifest: Manifest,
    app: FastAPI,
    *,
    prefix: str = "/",
    dependency_builder: DependencyBuilder | None = None,
    tags: Sequence[str] | None = None,
) -> None:
    """Mount routes + error handlers onto a user-owned FastAPI app.

    Does **not** wire the lifespan — the caller must ``async with manifest``
    inside their own lifespan.
    """
    register_error_handlers(app)
    for resource in manifest.resources:
        register_routes(
            app, resource, prefix=prefix, dependency_builder=dependency_builder, tags=tags
        )


def _lifespan(manifest: Manifest) -> Callable[[FastAPI], Any]:
    """An ASGI lifespan entering/exiting ``manifest`` around the app's life."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with manifest:
            yield

    return lifespan


def _configure_cors(app: FastAPI, cors_origins: list[str] | None) -> None:
    """Add CORS middleware when origins are configured.

    ``allow_credentials`` is forced to ``False`` for a wildcard origin: the CORS
    spec forbids credentialed responses with one, and Starlette does not rewrite
    it, so browsers would silently reject them.
    """
    if not cors_origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
