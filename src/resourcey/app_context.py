"""The trivial app-level context shared across resource lifecycles.

:class:`AppContext` is the shared holder a resource's async lifecycle
(:meth:`~resourcey.resource.base.BaseResource.lifespan`) reads from and
caches backend components on. It is deliberately **not** a DI container: it
is a typed cache (``get`` / ``set``) plus a list of async disposers run on
shutdown. The app factory builds one per app and enters each resource's
lifecycle with it.

The escape hatch: a caller pre-seeds the context before resources enter (e.g.
``ctx.set(_SESSION_FACTORY_KEY, my_factory)``) so a resource finds its
dependency already cached and skips building — the
``manifest.create_app(app_context=ctx)`` escape hatch, now backend-neutral.

Disposers are coroutines registered via :meth:`add_disposer`; :meth:`aclose`
runs them in reverse registration order. A caller-supplied (pre-seeded)
component is **not** registered for disposal — the caller owns its lifecycle,
mirroring the old ``engine_owned_by_app`` flag.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from resourcey.config.config_base import BaseConfig


class AppContext:
    """A typed cache + disposer list shared across resource lifecycles.

    Attributes:
        config: The active framework config. Resources read backend settings
            (e.g. ``config.database.database_url``) from here rather than
            importing the config module directly, keeping resource modules
            config-agnostic.
    """

    def __init__(self, config: BaseConfig) -> None:
        self.config = config
        self._cache: dict[Any, Any] = {}
        self._disposers: list[Callable[[], Awaitable[None]]] = []

    def get(self, key: Any) -> Any:
        """Return a cached value for ``key``, or ``None`` if not present."""
        return self._cache.get(key)

    def has(self, key: Any) -> bool:
        """Whether ``key`` has been seeded/cached."""
        return key in self._cache

    def set(self, key: Any, value: Any) -> None:
        """Cache ``value`` under ``key`` without registering a disposer.

        Use this for caller-supplied (escape-hatch) components the caller
        owns, or for resources that dispose themselves via their own
        ``__aexit__``.
        """
        self._cache[key] = value

    def set_with_disposer(
        self, key: Any, value: Any, disposer: Callable[[], Awaitable[None]]
    ) -> None:
        """Cache ``value`` under ``key`` and register ``disposer`` for shutdown.

        The disposer is a no-arg async callable (e.g. ``engine.dispose``).
        Disposal runs in reverse registration order on :meth:`aclose`.
        """
        self._cache[key] = value
        self.add_disposer(disposer)

    def add_disposer(self, disposer: Callable[[], Awaitable[None]]) -> None:
        """Register an async disposer to run on :meth:`aclose`."""
        self._disposers.append(disposer)

    async def aclose(self) -> None:
        """Run registered disposers in reverse order, clearing the cache.

        Errors in one disposer do not prevent later ones from running; the
        first exception is re-raised after all have been attempted.
        """
        first_exc: BaseException | None = None
        while self._disposers:
            disposer = self._disposers.pop()
            try:
                await disposer()
            except BaseException as exc:
                if first_exc is None:
                    first_exc = exc
        self._cache.clear()
        if first_exc is not None:
            raise first_exc
