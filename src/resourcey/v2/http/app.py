"""App assembly for ``v2``: :func:`create_app` and :func:`add_to_app`.

These are **free functions**, not methods on
:class:`~resourcey.v2.core.manifest.Manifest` — keeping HTTP out of ``v2/core``
honours the documented rule that HTTP construction is a transport concern, and
leaves core untouched. The function form is the extension point: later
concerns (the dependency builder of issue #86, auth, config) become additional
keyword arguments with no core change.

``create_app`` builds a fresh :class:`~fastapi.FastAPI` wired to the manifest's
lifespan (``async with manifest``), its routes, error handlers, and optional
CORS. ``add_to_app`` mounts the same routes + error handlers onto a
user-owned app and deliberately does **not** wire a lifespan — Starlette has a
single lifespan slot, so the caller composes it explicitly.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.routes import register_error_handlers, register_routes


def create_app(manifest: Manifest, *, cors_origins: list[str] | None = None) -> FastAPI:
    """Build a fresh FastAPI app wired to this manifest's lifecycle.

    Sugar: a fresh ``FastAPI`` with the manifest as its lifespan (``async with
    manifest``), routes + error handlers mounted, and CORS added when
    ``cors_origins`` is non-empty. For a custom lifespan or a pre-existing app,
    use :func:`add_to_app` and ``async with manifest`` manually.

    Args:
        manifest: The resource set to serve.
        cors_origins: Allowed CORS origins; empty/``None`` adds no middleware.
    """
    app = FastAPI(lifespan=_lifespan(manifest))
    _configure_cors(app, cors_origins)
    add_to_app(manifest, app)
    return app


def add_to_app(manifest: Manifest, app: FastAPI, *, prefix: str = "/") -> None:
    """Mount routes + error handlers onto a user-owned FastAPI app.

    Does **not** wire the lifespan — the caller must ``async with manifest``
    inside their own lifespan so Starlette's single-lifespan slot is composed
    explicitly.
    """
    register_error_handlers(app)
    for resource in manifest.resources:
        register_routes(app, resource, prefix=prefix)


def _lifespan(manifest: Manifest) -> Callable[[FastAPI], Any]:
    """An ASGI lifespan entering/exiting ``manifest`` around the app's life."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with manifest:
            yield

    return lifespan


def _configure_cors(app: FastAPI, cors_origins: list[str] | None) -> None:
    """Add CORS middleware when origins are configured.

    When ``cors_origins`` is empty no middleware is added (the app serves
    same-origin only). A wildcard ``["*"]`` is passed through verbatim, but
    ``allow_credentials`` is forced to ``False`` in that case: the CORS spec
    forbids credentialed responses with a wildcard origin, and Starlette does
    not rewrite it, so browsers would otherwise silently reject them.
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
