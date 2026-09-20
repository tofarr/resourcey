"""The app factory that assembles a runnable FastAPI application.

:func:`create_app` is the wiring layer (issue #21) that connects the
already-built pieces - config (:mod:`resourcey.config.config_runtime`),
:mod:`resourcey.resource.routes`, and the error envelope - into a single
process a developer can launch.

Config is read lazily via :func:`~resourcey.config.config_runtime.get_config`
(never at module import time). The factory is **storage-agnostic**: it builds
an :class:`~resourcey.app_context.AppContext` from config, then enters each
resource's :meth:`~resourcey.resource.base.BaseResource.lifespan` within a
Starlette lifespan. Each resource pulls its own backend dependencies (a SQL
engine, a Mongo client, ...) from the context — the factory no longer builds
SQL engines or calls ``configure``. It configures CORS middleware from
``config.cors_origins``, registers the error envelope, and mounts the REST
endpoints for each registered resource via
:func:`resourcey.resource.routes.register_routes`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.app_context import AppContext
from resourcey.config.config_base import BaseConfig
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import (
    get_config_as,
    set_config,
)
from resourcey.resource.base import BaseResource
from resourcey.resource.routes import register_error_handlers, register_routes
from resourcey.resource.sql import _SESSION_FACTORY_KEY, SqlResource


def create_app(
    *,
    resources: Sequence[type[BaseResource]] | None = None,
    config: BaseConfig | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    app_context: AppContext | None = None,
) -> FastAPI:
    """Assemble a runnable FastAPI application from config + resources.

    The factory is storage-agnostic: it does not build SQL engines or call
    ``configure``. Instead it builds an :class:`AppContext` from config and
    enters each resource's :meth:`~resourcey.resource.base.BaseResource.lifespan`
    within the app lifespan. Each resource pulls its own backend dependencies
    from the context.

    Args:
        resources: Explicit resource classes that win over ``config.resources``
            (the escape hatch). When ``None``, the resource set is read from
            ``config.resources``.
        config: A fully-built config instance. When supplied it is installed
            as the active config via :func:`set_config` so the override wins
            for the process; when omitted the active config is read via
            :func:`get_config`.
        session_factory: A pre-built SQL ``async_sessionmaker`` (convenience
            escape hatch). When supplied it is pre-seeded onto the app context
            so :meth:`SqlResource.lifespan` finds it cached and skips building
            an engine. For non-SQL backends or full control, pass
            ``app_context`` instead.
        app_context: A pre-built (and optionally pre-seeded)
            :class:`AppContext`. When supplied it wins over ``config`` for the
            context's config field; resources read everything from it. The
            ultimate escape hatch for any backend.

    Returns:
        A :class:`FastAPI` with CORS middleware, error handlers, a lifespan
        that enters each resource's lifecycle, and a route for each
        registered resource's standard actions.
    """
    if config is not None:
        set_config(config)
    active = get_config_as(FrameworkConfig)

    ctx = app_context if app_context is not None else AppContext(active)
    # Convenience escape hatch: pre-seed a caller-supplied SQL session factory
    # so SqlResource.lifespan reuses it instead of building an engine.
    if session_factory is not None:
        SqlResource._session_factory = session_factory
        ctx.set(_SESSION_FACTORY_KEY, session_factory)

    resolved_resources = list(resources) if resources is not None else list(active.resources)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Enter each resource's lifecycle within a shared exit stack so
        # teardown runs in reverse order. Resources build/cache their backend
        # connections here and register disposers on ``ctx`` for shutdown.
        # ``ctx.aclose`` is pushed as a stack callback (not a plain finally)
        # so disposers run even when a later resource's lifespan raises during
        # startup — otherwise its already-entered siblings' engines/clients
        # would leak.
        async with AsyncExitStack() as stack:
            stack.push_async_callback(ctx.aclose)
            for resource in resolved_resources:
                await stack.enter_async_context(resource.lifespan(ctx))
            yield

    app = FastAPI(lifespan=lifespan)
    _configure_cors(app, active.cors_origins)
    register_error_handlers(app)

    for resource in resolved_resources:
        register_routes(app, resource)

    return app


def _configure_cors(app: FastAPI, cors_origins: list[str]) -> None:
    """Add CORS middleware when origins are configured.

    When ``cors_origins`` is empty no middleware is added (the app serves
    same-origin only). A wildcard ``["*"]`` is passed through verbatim, but
    ``allow_credentials`` is forced to ``False`` in that case: the CORS spec
    forbids credentialed responses with a wildcard origin, and Starlette does
    not rewrite it, so browsers would otherwise silently reject them.
    """
    if not cors_origins:
        return
    allow_credentials = "*" not in cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
