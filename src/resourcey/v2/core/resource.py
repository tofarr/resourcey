"""``Resource`` — the declaration derived from a DTO.

A :class:`Resource` is *derived from* a :class:`~resourcey.v2.core.dto.DTO`: the
DTO is the field-level declaration, the resource serves it through a service.
The backend is chosen explicitly by the subclass, never inferred from app
config::

    manifest = Manifest(resources=[SqlResource(Thread, session_factory=factory)])

``ctx`` and storage ownership
-----------------------------
:meth:`Resource.get_service` is **sync** and takes an optional call-scoped
``MutableMapping``; the returned :class:`~resourcey.v2.core.service.Service` is
the async context manager that owns the storage lifetime. This keeps two
storage strategies expressible and privileges neither:

* **session-per-service** — the dependency caches a session in the call
  context, shares it across services, and commits/rolls back at the end;
* **session-per-operation** — the service holds a session factory and opens a
  session per action.

The rule that lets both coexist:

    Whoever opens the storage owns its commit and close. A resource that finds
    storage already in ``ctx`` reuses it and neither commits nor closes it.

``ctx`` is a plain ``MutableMapping`` keyed by module-level sentinels
(:data:`~resourcey.v2.core.service.STORAGE_KEY`), so an escape-hatch caller can
pre-seed storage and every resource in the call adopts it. ``AppContext``
(app-scoped: config, factories, clients) is a separate concept — ``ctx`` must
not absorb it.

``get_supported_actions()`` is the single action declaration (there is no
``actions`` property); exposure composes on top of it, and the exposed
resource's declaration wins outright.

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, MutableMapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from fastapi import Request
from pydantic import BaseModel

from resourcey.v2.core.dto import DTO, RestModels
from resourcey.v2.core.service import Action, CacheStrategy, Service, ServiceError

if TYPE_CHECKING:
    from resourcey.v2.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z]{2})(?=[A-Z][a-z])")


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


class Resource(Generic[T]):
    """A resource derived from a DTO declaration, served through a service.

    The base is storage-agnostic: it knows the DTO, the derived REST models,
    and the action surface, but not where the data lives. A backend subclasses
    it and implements :meth:`build_service`.
    """

    def __init__(self, dto: type[DTO], *, path: str | None = None) -> None:
        self._dto = dto
        self._path = path
        self._entered = False
        self._manifest: Manifest | None = None

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The generated DTO model — the single internal type to work with."""
        return cast(type[T], self._dto.get_dto_type())

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the DTO's ``in_*`` flags."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name, from the DTO declaration (``id`` by default)."""
        return self._dto.id_field_name

    def get_search_filter_type(self) -> type | None:
        """The declared search-filter class, or ``None`` for no filtering."""
        return None

    def get_cache_strategy(self) -> CacheStrategy | None:
        """The cache strategy for this resource, or ``None`` for no caching.

        ``v2/core`` itself stays dependency-free: the base returns ``None``.
        :class:`~resourcey.v2.sql.resource.SqlResource` overrides this to pick a
        concrete strategy (last-modified when the read model carries
        ``updated_at``, else ETag), and a developer overrides it to change the
        policy — the single seam for cache policy.
        """
        return None

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the DTO name, pluralized."""
        if self._path is not None:
            return self._path.lstrip("/")
        return _pluralize(_camel_to_kebab(self._dto.__name__).lower())

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        """The actions this resource serves — the single action declaration.

        Defaults to every :class:`Action`. A subclass narrows by overriding; it
        must never widen beyond ``frozenset(Action)``. The startup assertion in
        :class:`~resourcey.v2.core.manifest.Manifest` rejects non-``Action``
        members so a typo fails loudly instead of silently dropping a route.
        """
        return frozenset(Action)

    def get_exposed_resource(self) -> Resource[T] | None:
        """The resource the outside world sees (default: ``self``).

        ``None`` means internal-only. The exposed resource's
        :meth:`get_supported_actions` wins outright — no union with an inner
        resource.
        """
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T]:
        """Build the service for this resource over the call-scoped ``ctx``.

        Sync: the returned :class:`~resourcey.v2.core.service.Service` is the
        async context manager that opens (and, when it opened them, commits and
        closes) the storage. Pass ``ctx`` to share storage across services.
        """
        return self.build_service(ctx if ctx is not None else {})

    def build_service(self, ctx: MutableMapping[Any, Any]) -> Service[T]:
        """Build the service bound to ``ctx`` (backend seam; the base raises)."""
        raise ServiceError(
            f"{type(self).__name__} has no storage backend; subclass it (e.g. a SQL "
            "backend in resourcey.v2.sql) and implement build_service()."
        )

    async def get_service_dependency(self, request: Request) -> AsyncIterator[Service[T]]:
        """Yield the entered per-request service (usable directly as a FastAPI dependency).

        The per-request seam a route builder consumes. It resolves the
        request-scoped ``ctx`` (so every resource in one request shares
        storage), builds the service, and enters it for the caller.
        """
        service = self.get_service(_request_ctx(request))
        async with service:
            yield service

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        """Receive a reference to the :class:`~resourcey.v2.core.manifest.Manifest` that owns this resource.

        A **sync** notification (never a coroutine) called once by the manifest
        at construction, in declaration order. The base implementation simply
        records the reference, which :meth:`get_manifest` reads back; an
        override typically does the same (call ``super()``) and nothing else.

        Do **not** resolve sibling resources here: registration is not an
        ordering contract, so a lookup at this point would depend on where the
        manifest's iteration happens to be. Resolve siblings lazily, later,
        through :meth:`get_manifest` — e.g. when a request needs to verify a
        foreign key::

            manifest = self.get_manifest()
            if manifest is not None:
                threads = manifest.get_resource("threads")
        """
        self._manifest = manifest

    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this resource, or ``None`` if unregistered."""
        return self._manifest

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Resource[T]:
        """Enter the resource's runtime lifecycle (guards against double entry)."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the resource's runtime lifecycle."""
        self._entered = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_ctx(request: Request) -> MutableMapping[Any, Any]:
    """The call-scoped context for ``request`` (created on first use)."""
    ctx = getattr(request.state, "_v2_ctx", None)
    if ctx is None:
        ctx = {}
        request.state._v2_ctx = ctx
    return cast(MutableMapping[Any, Any], ctx)


def _camel_to_kebab(name: str) -> str:
    """Insert ``-`` boundaries into a CamelCase identifier (kept local to core)."""
    return _CAMEL_BOUNDARY.sub("-", name)


def _pluralize(name: str) -> str:
    """Append a simple English plural suffix (kept local to core)."""
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"
