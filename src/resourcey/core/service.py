"""``Service`` — the storage-agnostic action contract — plus ``Action`` and friends.

A service exposes the standard resource actions (create, read, update, delete,
search, count, batch_read, batch_edit) as async methods. It is generic over the
DTO type it serves (``T``) and the identifier type (``K``), so it takes DTOs /
ids in and declares DTOs as its return types.

The service is the **async context manager**: it owns its storage lifetime, not
the resource. Two storage strategies are both expressible and core privileges
neither (see :class:`~resourcey.core.resource.Resource`):

* session-per-service — a caller supplies the storage via ``ctx`` and owns
  commit/close;
* session-per-operation — the service opens and closes its own storage.

The base class holds no storage. Concrete services override the actions they
support; the raising defaults are a safety net so a misconfigured route
surfaces clearly rather than silently doing nothing.

``search`` / ``count`` take a standard
:class:`~resourcey.util.search_filter.SearchFilter` tree and ``search`` a
:class:`~resourcey.util.sort_order.SortOrder`; both are passed as plain
arguments, so the whole operation's inputs are explicit rather than wrapped in
a request object. ``batch_edit`` takes a list of :class:`Edit` nodes so a single
batch can create, update, *and* delete.

This module is part of the ``core`` layer: besides the standard library it
imports only ``util`` (the filter / sort / discriminated-union leaves) and
its sibling ``core.errors`` (the framework error base) — ``core`` imports
no other project package.
"""

from __future__ import annotations

import enum
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import SkipValidation, field_validator

from resourcey.core.errors import ResourceyError
from resourcey.util.models import DiscriminatedUnionMixin
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")

# The default page size for ``search`` (and :class:`Page`). The transport's
# ``limit`` default also reads this, so a direct service call and an HTTP call
# paginate alike.
DEFAULT_LIMIT = 20


class Action(enum.StrEnum):
    """The standard resource actions.

    Member *names* (lowercased) match the :class:`Service` method names exactly
    (``CREATE`` -> ``create``, ``BATCH_READ`` -> ``batch_read``) so a route
    builder can derive a method name from an :class:`Action` directly. Being a
    ``StrEnum``, ``Action("create") in supported`` works for a correctly
    spelled string, which is why a startup assertion — not mypy — is what
    catches a typo in a dynamically built action set.
    """

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    SEARCH = "search"
    COUNT = "count"
    BATCH_READ = "batch_read"
    BATCH_EDIT = "batch_edit"


# Call-scoped context key under which a service caches the storage it opened.
# A plain ``object()`` so two independent modules cannot collide on a string;
# module scope so every resource in the process shares the one key.
STORAGE_KEY: Any = object()


class ServiceError(ResourceyError):
    """A service is misconfigured (e.g. used before it was entered)."""


class NotFoundError(ResourceyError):
    """A requested entity does not exist (the transport layer maps it to 404)."""

    def __init__(self, id: Any) -> None:  # noqa: A002
        super().__init__(f"No entity with id {id!r}")
        self.id = id


class ForbiddenError(ResourceyError):
    """An attempted action is not permitted (the transport layer maps it to 403).

    Authorization raises this for an action with no permitted scope — a denied
    ``create`` is the clear case. By-id actions deliberately raise
    :class:`NotFoundError` instead, so an out-of-scope id is indistinguishable
    from an absent one and existence does not leak.
    """

    def __init__(self, resource_name: str, action: str) -> None:
        super().__init__(f"{action} is not permitted on {resource_name!r}")
        self.resource_name = resource_name
        self.action = action


class CacheStrategy:
    """A cache policy placeholder: the ``core`` layer names the concept only.

    Concrete strategies live in ``resourcey.cache``; ``core`` deliberately
    does not depend on them. A migrated resource supplies its own strategy and
    the transport layer interprets it. The methods below are the whole contract a
    concrete strategy must satisfy: a header for a list of read models, one for a
    bare count, and the *programmatic* freshness question (whether a cached copy
    a server-side cache holds is still good). All three are typed ``Any`` /
    primitive so ``core`` stays free of the concrete ``CacheHeader`` type.
    """

    def get_cache_header(self, models: list[Any], *, context: dict[str, Any] | None = None) -> Any:
        """Compute a transport-specific cache header for ``models`` (default: none)."""
        return None

    def count_cache_header(self, count: int, filters: Any = None) -> Any:
        """Compute a cache header for a bare ``count`` result (default: none)."""
        return None

    async def should_read(
        self,
        read_at: datetime | None = None,
        etag: str | None = None,
    ) -> bool:
        """Whether a fresh read from the source is required (default: always).

        The *programmatic* counterpart to the HTTP-facing header methods: a
        server-side cache asks "is the copy I hold still good?" and re-reads the
        source only when this returns ``True``. ``read_at`` is when the cache
        entry was written and ``etag`` the validator it holds. Async so a
        strategy that must consult storage can without a later signature change.

        The base returns ``True`` — no policy, so always read.
        """
        return True


@dataclass
class Page(Generic[T]):
    """A page of cursor-paginated search results.

    ``next_cursor`` is an opaque keyset cursor pointing at the last item of
    this page; pass it as the ``cursor`` on the next call to fetch the
    following page. It is ``None`` when this page is the last.
    """

    items: list[T] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Edits - the batch_edit item union
# ---------------------------------------------------------------------------


class Edit(DiscriminatedUnionMixin, ABC):
    """One item of a ``batch_edit``: a create, an update, or a delete.

    The union is discriminated by ``kind`` (the class name, from
    :class:`~resourcey.util.models.DiscriminatedUnionMixin`), so a single
    batch can mix all three operations.

    It is generic over the DTO type ``T`` and the identifier type ``K``; a
    backend builds the concrete nodes with those parameters bound to its own
    types. The base is abstract, so only the three nodes below are instantiable.
    """


class Create(Edit, Generic[T]):
    """A ``batch_edit`` item that creates a new entity from ``item``."""

    item: T


# A condition is the *native* standard :class:`SearchFilter` tree, carried as a
# ``SkipValidation`` field so an already-built instance (the service-caller path)
# is not re-entered through the discriminated-union validator; a wire ``dict``
# (the transport path) is resolved by each node's ``mode="before"`` validator.
# A parameterised generic annotation (``SearchFilter[Any]``) would re-enter the
# union validator on an instance, the same reason ``util.search_filter`` uses
# ``NestedFilter`` for its nested fields. The ``| None`` union is accepted by
# ``SkipValidation`` (the bare annotation is not).
Condition = Annotated[SearchFilter[Any] | None, SkipValidation]


def _resolve_condition(value: Any) -> Any:
    """Resolve a wire ``dict`` condition into a filter instance, else pass through."""
    if isinstance(value, dict):
        return SearchFilter.model_validate(value)
    return value


class Update(Edit, Generic[T]):
    """A ``batch_edit`` item that updates the entity identified by ``item``.

    ``condition`` is an optional :class:`SearchFilter` tree the stored row must
    satisfy for the update to apply; ``None`` means no condition. A failed
    condition is indistinguishable from an absent row (the update writes
    nothing and yields ``None``).
    """

    item: T
    condition: Condition = None

    @field_validator("condition", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_condition(value)


class Delete(Edit, Generic[K]):
    """A ``batch_edit`` item that deletes the entity identified by ``id``.

    ``condition`` is an optional :class:`SearchFilter` tree the stored row must
    satisfy for the delete to apply; ``None`` means no condition. A failed
    condition is indistinguishable from an absent row (nothing is deleted).
    """

    id: K
    condition: Condition = None

    @field_validator("condition", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_condition(value)


class Service(Generic[T, K]):
    """The storage-agnostic service contract, generic over the DTO ``T`` and id ``K``.

    The service is its own async context manager: it owns the lifetime of any
    storage it opens. An action method called before ``__aenter__`` raises
    :class:`ServiceError` rather than operating on half-initialised state, and
    re-entering an already-entered service raises too.

    Subclasses override the actions the resource exposes. They may override
    ``__aenter__`` / ``__aexit__`` to open and close storage, but must call
    ``super()`` so the entered-guard stays intact.
    """

    def __init__(self) -> None:
        self._entered = False
        self._serialization_ctx: dict[str, Any] | None = None
        self._response_private = False

    # ------------------------------------------------------------------
    # Serialization context
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The context threaded into ``model_dump`` / ``model_validate``, or ``None``.

        The transport passes this to the projection / serialization of every
        response, so a secret-bearing field serializes exactly as the context
        dictates (redacted by default; plaintext under ``expose_secrets``; JWE
        under an ``encryption_service``). ``None`` means "no context" — the
        redacting default. A service that must reveal a secret (e.g. a one-time
        minted key) returns ``{"expose_secrets": True}`` here.
        """
        return self._serialization_ctx

    def set_serialization_context(self, ctx: dict[str, Any] | None) -> None:
        """Set the serialization context this service supplies to the transport."""
        self._serialization_ctx = ctx

    # ------------------------------------------------------------------
    # Cache privacy
    # ------------------------------------------------------------------

    def response_is_private(self) -> bool:
        """Whether this response may differ per caller, so it must not be shared.

        ``False`` by default. A service wrapping a *principal-scoped* resource —
        one whose responses depend on who is asking (an authorization policy that
        narrows rows, e.g. an ``Owner`` policy) — returns ``True``, and the
        transport then marks the response ``Cache-Control: private`` so a shared
        cache (proxy / CDN) neither stores nor revalidates it for another caller.

        This is the service-level half of the "a row-scoping policy must force a
        caller-private cache" invariant: an ``ETag`` / ``Last-Modified`` alone
        cannot keep a shared cache from serving one principal's slice to another.
        """
        return self._response_private

    def set_response_private(self, private: bool) -> None:
        """Mark whether this service's responses are caller-scoped.

        See :meth:`response_is_private`. A wrapper (e.g. an authorization service)
        that knows it enforces a principal-dependent policy calls this on its own
        instance; it is not inherited by a delegate.
        """
        self._response_private = private

    # ------------------------------------------------------------------
    # Lifecycle (the service is the async context manager)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Service[T, K]:
        """Mark the service entered and return it."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Mark the service exited."""
        self._entered = False

    @property
    def entered(self) -> bool:
        """Whether the service is currently inside its ``async with`` block."""
        return self._entered

    def _require_entered(self) -> None:
        """Raise a clear error when an action is used outside its ``async with``."""
        if not self._entered:
            raise ServiceError(
                f"{type(self).__name__} was used before entering it; call it inside "
                "'async with service:' so it can open (and later close) its storage."
            )

    # ------------------------------------------------------------------
    # Standard actions (raising defaults — override the supported ones)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        raise NotImplementedError

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        raise NotImplementedError

    async def update(self, payload: T, *, condition: SearchFilter[Any] | None = None) -> T | None:
        """Apply an update DTO (which carries its own identifier); return the DTO.

        ``condition`` is an optional :class:`SearchFilter` tree the stored row
        must satisfy for the write to apply. The result is ``None`` whenever no
        row was written — an absent id or a failed condition — so the two are
        deliberately indistinguishable and a failed condition never leaks that
        the row exists.
        """
        self._require_entered()
        raise NotImplementedError

    async def delete(self, id: K, *, condition: SearchFilter[Any] | None = None) -> bool:  # noqa: A002
        """Delete by id; return whether a row was deleted.

        ``condition`` is an optional :class:`SearchFilter` tree the stored row
        must satisfy for the delete to apply. ``False`` means no row was deleted
        — an absent id or a failed condition — so the two are deliberately
        indistinguishable and a failed condition never leaks that the row exists.
        """
        self._require_entered()
        raise NotImplementedError

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        """Return a page of ``T`` matching ``search_filter``, ordered by ``sort_order``.

        ``sort_order`` of ``None`` is the default identifier order; ``cursor`` is
        an opaque keyset cursor from a previous page (``None`` for the first) and
        ``limit`` bounds the page size.
        """
        self._require_entered()
        raise NotImplementedError

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        """Return the number of ``T`` matching ``search_filter`` (all when ``None``)."""
        self._require_entered()
        raise NotImplementedError

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        self._require_entered()
        raise NotImplementedError

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Apply a list of :class:`Edit` nodes; results align positionally with ``edits``.

        A :class:`Create` / :class:`Update` yields the resulting DTO, a
        :class:`Delete` yields ``None`` (nothing to return), and a miss — an
        absent id, or an :class:`Update` / :class:`Delete` whose ``condition``
        the stored row no longer satisfies — also yields ``None``.
        """
        self._require_entered()
        raise NotImplementedError


def assert_real_actions(name: str, actions: frozenset[Any]) -> None:
    """Assert every member of a dynamically built action set is a real :class:`Action`.

    ``Action`` is a ``StrEnum``, so a correctly spelled string compares equal to
    a member and a typo silently drops a route rather than failing. This is the
    startup check that turns that silent drop into a loud error.
    """
    unknown = sorted(str(a) for a in actions if not isinstance(a, Action))
    if unknown:
        raise ServiceError(
            f"{name}.get_supported_actions() contained {unknown}; every member must be an "
            "Action (a typo would otherwise silently drop a route)."
        )


# The singular actions a batch action is a batch *of*. A batch action cannot
# outlive the singular action it batches: batch_read reads, so it needs read;
# batch_edit edits, so it needs at least one of create / update / delete.
_BATCH_PREREQUISITES: dict[Action, frozenset[Action]] = {
    Action.BATCH_READ: frozenset({Action.READ}),
    Action.BATCH_EDIT: frozenset({Action.CREATE, Action.UPDATE, Action.DELETE}),
}


def normalize_actions(actions: frozenset[Action]) -> frozenset[Action]:
    """Drop any batch action whose singular action is absent.

    A batch action is only meaningful when the singular action it batches is
    also exposed: ``batch_read`` reads (so it requires ``read``), and
    ``batch_edit`` creates / updates / deletes (so it requires at least one of
    those). Normalising here — rather than raising — means a caller who narrows
    a surface by removing, say, only ``read`` or only ``update`` gets a coherent
    action set without having to remember to also remove the batch action.

    Idempotent, so it is safe to apply at every layer that consumes a narrowed
    action set (the route builder, the backends' ``batch_edit``, and a
    ``ResourceView``).
    """
    normalized = set(actions)
    for batch_action, prerequisites in _BATCH_PREREQUISITES.items():
        if batch_action in normalized and normalized.isdisjoint(prerequisites):
            normalized.discard(batch_action)
    return frozenset(normalized)


# The singular action a derived action reduces to for a policy that reasons only
# about the CRUD surface: counting is not a separate privilege from searching,
# and batching is not a separate privilege from the action it batches. A venue
# that *does* distinguish them (a wrapper's action set, a view) uses the actions
# as declared; this is for the policy reduction alone.
_ACTION_EQUIVALENT: dict[Action, Action] = {
    Action.COUNT: Action.SEARCH,
    Action.BATCH_READ: Action.READ,
    Action.BATCH_EDIT: Action.UPDATE,
}


def normalize_action(action: Action) -> Action:
    """Reduce ``COUNT`` / ``BATCH_*`` to their closest singular CRUD action.

    ``COUNT`` reuses the ``SEARCH`` permission, ``BATCH_READ`` reuses ``READ``,
    and ``BATCH_EDIT`` reuses ``UPDATE`` — the same equivalences
    :func:`normalize_actions` uses to prune a batch action whose singular action
    is absent. A policy may instead list the derived members it grants
    explicitly; either way the reduction agrees.
    ``CREATE`` / ``READ`` / ``UPDATE`` / ``DELETE`` / ``SEARCH`` are unchanged.
    """
    return _ACTION_EQUIVALENT.get(action, action)
