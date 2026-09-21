"""The application-owned resource manifest (issue #51).

A :class:`ResourceManifest` is a frozen declaration of the resources an app
serves. The app author constructs it with resource **types**; the manifest owns
the resource **instances** and their lifecycle.

    manifest = ResourceManifest(resources=(Thread, Message))
    app = manifest.create_app()

For a user who owns their own FastAPI app:

    manifest = ResourceManifest(resources=(Thread, Message))
    @asynccontextmanager
    async def lifespan(app):
        async with manifest:
            yield
    app = FastAPI(lifespan=lifespan)
    manifest.add_to_app(app, prefix="/api")

Construction instantiates each type and calls ``on_register`` (SQL resources
build their ORM model so the table lands in metadata before migrations run).
``__aenter__`` / ``__aexit__`` enter/exit each instance's runtime lifecycle
(build/dispose connections) in declaration order / reverse.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from resourcey.app_context import AppContext
from resourcey.config.config_base import BaseConfig
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_runtime import get_config_as, set_config
from resourcey.resource.base import BaseResource
from resourcey.resource.routes import register_error_handlers, register_routes


class ResourceManifest(BaseModel):
    """Frozen declaration of an app's resources, owning their instances.

    Attributes:
        resources: Resource **types** (subclasses of :class:`BaseResource`),
            in declaration order. The manifest instantiates each on
            construction and calls ``on_register`` so backend artifacts (SQL
            models, etc.) are materialised.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    resources: tuple[type[BaseResource], ...]

    # Instance state set after validation. Non-annotated so Pydantic ignores
    # them; stored on the instance via model_post_init.
    _instances: tuple[BaseResource, ...]
    _ctx: AppContext
    _entered: bool

    def model_post_init(self, _context: object) -> None:
        """Instantiate each resource type and call ``on_register``."""
        instances: list[BaseResource] = []
        for cls in self.resources:
            instance = cls()
            instance.on_register()
            instances.append(instance)
        object.__setattr__(self, "_instances", tuple(instances))
        object.__setattr__(self, "_entered", False)

    @property
    def instances(self) -> tuple[BaseResource, ...]:
        """The materialised resource instances (in declaration order)."""
        return self._instances

    def materialize(self) -> None:
        """Ensure backend artifacts are built (idempotent, for migrations).

        ``on_register`` was already called at construction; this re-invokes it
        so a manifest imported by ``env.py`` (without entering the lifecycle)
        has its tables in metadata before Alembic diffs.
        """
        for instance in self._instances:
            instance.on_register()

    async def __aenter__(self) -> AppContext:
        """Enter each resource's runtime lifecycle.

        Builds an :class:`AppContext` (or reuses the one set via
        ``create_app(app_context=...)``), then enters each instance in
        declaration order. Guards against double-entry.
        """
        if self._entered:
            raise RuntimeError("ResourceManifest already entered")
        object.__setattr__(self, "_entered", True)
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            ctx = AppContext(get_config_as(FrameworkConfig))
            object.__setattr__(self, "_ctx", ctx)
        for instance in self._instances:
            await instance.__aenter__(ctx)
        return ctx

    async def __aexit__(self, *exc: object) -> None:
        """Exit each resource in reverse order, then run context disposers."""
        ctx = getattr(self, "_ctx", None)
        first_exc: BaseException | None = None
        for instance in reversed(self._instances):
            try:
                await instance.__aexit__(*exc)
            except BaseException as ex:
                if first_exc is None:
                    first_exc = ex
        if ctx is not None:
            try:
                await ctx.aclose()
            except BaseException as ex:
                if first_exc is None:
                    first_exc = ex
        object.__setattr__(self, "_entered", False)
        if first_exc is not None:
            raise first_exc

    def create_app(
        self,
        *,
        config: BaseConfig | None = None,
        app_context: AppContext | None = None,
    ) -> FastAPI:
        """Build a fresh FastAPI app wired to this manifest's lifecycle.

        Sugar: fresh ``FastAPI``, manifest as the lifespan, routes + error
        handlers + CORS mounted. For a custom lifespan or a pre-existing app,
        use :meth:`add_to_app` and ``async with manifest`` manually.

        Args:
            config: Override installed as the active config via :func:`set_config`.
            app_context: Pre-built (optionally pre-seeded) context. Resources
                read everything from it — the escape hatch for any backend.
        """
        if config is not None:
            set_config(config)
        active = get_config_as(FrameworkConfig)
        ctx = app_context if app_context is not None else AppContext(active)
        object.__setattr__(self, "_ctx", ctx)

        manifest = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            async with manifest:
                yield

        app = FastAPI(lifespan=lifespan)
        _configure_cors(app, active.cors_origins)
        register_error_handlers(app)
        for instance in self._instances:
            register_routes(app, instance)
        return app

    def add_to_app(self, app: FastAPI, *, prefix: str = "/") -> None:
        """Mount routes + error handlers onto a user-owned FastAPI app.

        Does **not** wire the lifespan — the user must ``async with manifest``
        inside their own lifespan so Starlette's single-lifespan slot is
        composed explicitly.
        """
        register_error_handlers(app)
        for instance in self._instances:
            register_routes(app, instance, prefix=prefix)


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
