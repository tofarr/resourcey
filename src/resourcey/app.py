"""The app factory that assembles a runnable FastAPI application.

:func:`create_app` is the wiring layer (issue #21) that connects the
already-built pieces — config (:mod:`resourcey.config.config_runtime`),
:class:`~resourcey.resource.service.ResourceService`, and the error envelope
— into a single process a developer can launch.

Config is read lazily via :func:`~resourcey.config.config_runtime.get_config`
(never at module import time). The factory builds an async engine +
``async_sessionmaker`` from ``config.database.database_url``, wires a
Starlette lifespan that owns the engine lifecycle (create/dispose only —
structured so lifecycle hooks (#15) can be added later), configures CORS
middleware from ``config.cors_origins``, registers the error envelope, and
mounts the REST endpoints for each registered resource via
:class:`ResourceService`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from resourcey.config.config_base import BaseConfig
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import (
    get_config_as,
    set_config,
)
from resourcey.resource.base import BaseResource
from resourcey.resource.service import ResourceService, register_error_handlers


def create_app(
    *,
    resources: Sequence[type[BaseResource]] | None = None,
    config: BaseConfig | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Assemble a runnable FastAPI application from config + resources.

    Args:
        resources: Explicit resource classes that win over ``config.resources``
            (the escape hatch). When ``None``, the resource set is read from
            ``config.resources``.
        config: A fully-built config instance. When supplied it is installed
            as the active config via :func:`set_config` so the override wins
            for the process; when omitted the active config is read via
            :func:`get_config`.
        engine: A pre-built async engine (escape hatch). When ``None`` an
            engine is built from ``config.database.database_url``. Mutually
            derived with ``session_factory`` — supplying either one causes the
            other to be built from it if missing.
        session_factory: A pre-built ``async_sessionmaker`` (escape hatch).
            When ``None`` one is built from the (resolved) engine.

    Returns:
        A :class:`FastAPI` with CORS middleware, error handlers, engine/session
        lifecycle (lifespan), and a route for each registered resource's
        standard actions.
    """
    if config is not None:
        set_config(config)
    active = get_config_as(FrameworkConfig)

    engine_owned_by_app = engine is None and session_factory is None
    resolved_engine, resolved_factory = _resolve_engine_and_factory(
        active.database.database_url, engine, session_factory
    )

    resolved_resources = list(resources) if resources is not None else list(active.resources)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Only dispose an engine the app built. A caller-supplied engine
            # or session factory owns its own lifecycle.
            if engine_owned_by_app and resolved_engine is not None:
                await resolved_engine.dispose()

    app = FastAPI(lifespan=lifespan)
    _configure_cors(app, active.cors_origins)
    register_error_handlers(app)

    for resource in resolved_resources:
        service = ResourceService(resource, session_factory=resolved_factory)
        service.register(app)

    return app


def _resolve_engine_and_factory(
    database_url: str,
    engine: AsyncEngine | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> tuple[AsyncEngine | None, async_sessionmaker[AsyncSession]]:
    """Build the engine + session factory pair, honouring either escape hatch.

    Returns ``(engine, factory)``. ``engine`` is ``None`` only when the caller
    supplied a ``session_factory`` with no engine — in that case the app does
    not own an engine and the lifespan skips disposal. A supplied ``engine``
    with no ``session_factory`` builds the factory from it. When neither is
    supplied both are built from ``database_url``.
    """
    if engine is not None and session_factory is not None:
        return engine, session_factory
    if engine is not None:
        return engine, async_sessionmaker(engine, expire_on_commit=False)
    if session_factory is not None:
        return _engine_from_factory(session_factory), session_factory
    built_engine = create_async_engine(database_url)
    return built_engine, async_sessionmaker(built_engine, expire_on_commit=False)


def _engine_from_factory(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncEngine | None:
    """Best-effort: recover the engine bound to a session factory, if any."""
    bound = getattr(factory, "bind", None)
    return bound if isinstance(bound, AsyncEngine) else None


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
