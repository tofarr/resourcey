"""``Resource`` — the abstract, storage-agnostic resource contract.

A :class:`Resource` is a genuine abstract base class: every method is abstract
and a backend supplies all of them. ``v2/core`` keeps no behaviour here — the
DTO / REST-model surface, the action declaration, exposure, the service seam,
the manifest reference, and the runtime lifecycle are all contract, so a
backend decides how each is satisfied. That keeps core free of any storage or
transport assumption and makes the resource a clean seam to subclass::

    manifest = Manifest(resources=[SqlResource(Thread, session_factory=factory)])

``ctx`` and storage ownership
-----------------------------
:meth:`Resource.get_service` is **sync** and takes an optional call-scoped
``MutableMapping``; the returned :class:`~resourcey.v2.core.service.Service` is
the async context manager that owns the storage lifetime. This keeps storage
strategies expressible without core privileging one:

* **session-per-service** — a caller supplies the storage via ``ctx`` and owns
  commit/close;
* **session-per-operation** — the service opens and closes its own storage.

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
``resourcey`` module. It deliberately knows nothing about HTTP — resolving a
per-request service dependency is a transport concern and lives in
``resourcey.v2.http``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from resourcey.v2.core.dto import RestModels
from resourcey.v2.core.service import Action, CacheStrategy, Service

if TYPE_CHECKING:
    from resourcey.v2.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z]{2})(?=[A-Z][a-z])")


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


class Resource(ABC, Generic[T]):
    """The abstract resource contract, generic over the DTO type ``T``.

    Every method is abstract. A backend (e.g.
    :class:`~resourcey.v2.sql.resource.SqlResource`) implements the whole
    contract; the transport layer consumes it, and the manifest owns the
    lifecycle it exposes through :meth:`__aenter__` / :meth:`__aexit__`.
    """

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    @abstractmethod
    def get_dto_type(self) -> type[T]:
        """The DTO model — the single internal type the service works with."""

    @abstractmethod
    def get_rest_models(self) -> RestModels:
        """The six REST models the transport projects onto."""

    @abstractmethod
    def get_id_field(self) -> str:
        """The identifier field name."""

    @abstractmethod
    def get_resource_path(self) -> str:
        """The REST path segment for this resource."""

    @abstractmethod
    def get_cache_strategy(self) -> CacheStrategy | None:
        """The cache strategy for this resource, or ``None`` for no caching."""

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    @abstractmethod
    def get_supported_actions(self) -> frozenset[Action]:
        """The actions this resource serves — the single action declaration."""

    @abstractmethod
    def get_exposed_resource(self) -> Resource[T] | None:
        """The resource the outside world sees, or ``None`` for internal-only."""

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    @abstractmethod
    def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T]:
        """Build the service for this resource over the call-scoped ``ctx``.

        Sync: the returned :class:`~resourcey.v2.core.service.Service` is the
        async context manager that owns the storage lifetime. Pass ``ctx`` to
        share storage across services.
        """

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    @abstractmethod
    def on_register(self, manifest: Manifest) -> None:
        """Receive a reference to the manifest that owns this resource.

        A **sync** notification (never a coroutine) called once by the manifest
        at construction, in declaration order. Sibling resources are resolved
        lazily, later, through :meth:`get_manifest` — never from inside this
        hook, since registration ordering is not a contract.
        """

    @abstractmethod
    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this resource, or ``None`` if unregistered."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def __aenter__(self) -> Resource[T]:
        """Enter the resource's runtime lifecycle."""

    @abstractmethod
    async def __aexit__(self, *exc: object) -> None:
        """Exit the resource's runtime lifecycle."""


# ---------------------------------------------------------------------------
# Name helpers shared by backends
# ---------------------------------------------------------------------------


def _camel_to_kebab(name: str) -> str:
    """Insert ``-`` boundaries into a CamelCase identifier (kept local to core)."""
    return _CAMEL_BOUNDARY.sub("-", name)


def _pluralize(name: str) -> str:
    """Append a simple English plural suffix (kept local to core)."""
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"
