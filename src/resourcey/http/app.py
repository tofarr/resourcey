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
from resourcey.http.batch_routes import DEFAULT_BATCH_PATH, register_batch_routes
from resourcey.http.dependency_builder import DependencyBuilder
from resourcey.http.routes import register_error_handlers, register_routes


def create_app(
    manifest: Manifest,
    *,
    cors_origins: list[str] | None = None,
    dependency_builder: DependencyBuilder | None = None,
    tags: Sequence[str] | None = None,
    batch_edit: bool = True,
    batch_edit_path: str = DEFAULT_BATCH_PATH,
    batch_edit_max_edits: int | None = None,
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
        batch_edit: Mount the app-level ``POST /batch-edit`` that spans every
            exposed resource. On by default; ``False`` removes that write surface.
        batch_edit_path: The path for that route (default ``/batch-edit``).
        batch_edit_max_edits: Cap on nodes in one batch. ``None`` (the default)
            reads ``RequestLimitsConfig.max_batch_edits`` — ``APP_MAX_BATCH_EDITS``,
            default 100. A cross-resource batch is the easiest way to turn one
            request into unbounded work, so it is never uncapped.
    """
    app = FastAPI(lifespan=_lifespan(manifest))
    _configure_cors(app, cors_origins)
    add_to_app(
        manifest,
        app,
        dependency_builder=dependency_builder,
        tags=tags,
        batch_edit=batch_edit,
        batch_edit_path=batch_edit_path,
        batch_edit_max_edits=batch_edit_max_edits,
    )
    return app


def add_to_app(
    manifest: Manifest,
    app: FastAPI,
    *,
    prefix: str = "/",
    dependency_builder: DependencyBuilder | None = None,
    tags: Sequence[str] | None = None,
    batch_edit: bool = True,
    batch_edit_path: str = DEFAULT_BATCH_PATH,
    batch_edit_max_edits: int | None = None,
) -> None:
    """Mount routes + error handlers onto a user-owned FastAPI app.

    Does **not** wire the lifespan — the caller must ``async with manifest``
    inside their own lifespan.

    ``batch_edit`` / ``batch_edit_path`` / ``batch_edit_max_edits`` control the
    app-level cross-resource ``POST /batch-edit``; see :func:`create_app`.
    """
    register_error_handlers(app)
    for resource in manifest.resources:
        register_routes(
            app, resource, prefix=prefix, dependency_builder=dependency_builder, tags=tags
        )
    if batch_edit:
        register_batch_routes(
            app,
            manifest,
            path=batch_edit_path,
            prefix=prefix,
            dependency_builder=dependency_builder,
            max_edits=batch_edit_max_edits,
            tags=tags,
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
