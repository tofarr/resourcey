"""Request-scoped async session dependency for the auth feature (issue #4).

resourcey does not have a global ``SessionDep`` like ohev2 — sessions are
managed per-resource via ``SqlResource.open_storage``. The auth layer needs
its own DB access (user lookups, IdP token persistence, OAuth client CRUD),
so this module provides a FastAPI dependency that:

1. Reuses ``request.state.session`` if another resource already opened one
   (so auth and resource operations share a single transaction).
2. Otherwise opens a new session from the session factory stored on
   ``app.state.resourcey_session_factory`` (set by the manifest's lifespan).
3. If no factory is on ``app.state``, builds one from config and caches it
   (the self-contained fallback when no SQL resource is registered).

The session is committed on success and rolled back on error, matching the
``_open_sql_service`` pattern in :mod:`resourcey.resource.sql`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as

_SESSION_FACTORY_STATE_KEY = "resourcey_session_factory"
_ENGINE_STATE_KEY = "resourcey_engine"


def _get_or_create_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Get the session factory from ``app.state``, building one if absent."""
    app = request.app
    factory = getattr(app.state, _SESSION_FACTORY_STATE_KEY, None)
    if factory is not None:
        return factory  # type: ignore[no-any-return]
    cfg = get_config_as(FrameworkConfig)
    engine = create_async_engine(cfg.database.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    setattr(app.state, _SESSION_FACTORY_STATE_KEY, factory)
    setattr(app.state, _ENGINE_STATE_KEY, engine)
    return factory


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield a request-scoped session.

    Reuses ``request.state.session`` if present (opened by another resource
    in the same request); otherwise opens a new one. The caller that opened
    the session owns the commit/close; a session opened here is committed on
    success and rolled back on error.
    """
    session = getattr(request.state, "session", None)
    if session is not None:
        yield session
        return
    factory = _get_or_create_factory(request)
    async with factory() as session:
        request.state.session = session
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def dispose_app_engine(app: Any) -> None:
    """Dispose the engine cached on ``app.state`` (for tests / shutdown)."""
    engine = getattr(app.state, _ENGINE_STATE_KEY, None)
    if engine is not None:
        await engine.dispose()
        delattr(app.state, _ENGINE_STATE_KEY)
    setattr(app.state, _SESSION_FACTORY_STATE_KEY, None)
