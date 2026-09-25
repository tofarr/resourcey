"""A :class:`Singleton` mixin for process-wide instances.

Several pieces of the framework are naturally one-per-process (a dependency
builder, a cache, the search-filter leaves), and each would otherwise
hand-roll an ``lru_cache`` accessor or its own ``__new__`` cache. This module
gives them one small, composable building block.

Semantics:

* Constructing a class more than once returns the **same** instance — identity,
  not equality.
* ``__init__`` runs **exactly once** per concrete class, on first construction.
  Later constructions with different arguments return the existing instance and
  leave it untouched, so the first construction wins.
* Each concrete subclass owns its own singleton: a base and its subclass are
  independent, as are sibling subclasses.

It is deliberately transport / framework agnostic and composes with both
Pydantic's ``BaseModel`` and :class:`~resourcey.v2.util.models.DiscriminatedUnionMixin`:
pydantic's validation/construction still runs (``model_validate`` returns the
cached instance), and one singleton is kept per discriminated ``kind``. No
extra Pydantic field is introduced — the cache lives on the class's own
``__dict__`` under private attribute names.

This module is part of ``v2/``: it imports nothing but the standard library.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Self, cast

_INSTANCE_ATTR = "_singleton_instance"
_INITIALIZED_ATTR = "_singleton_initialized"
_GUARDED_ATTR = "_singleton_guarded"

# A single re-entrant lock guards both instance creation and the one-time
# initialization. RLock (not Lock) because a subclass's __init__ reaches the
# guard again through super().__init__ on the same thread.
_LOCK = threading.RLock()

# Per-thread set of classes currently running their one-time __init__, so a
# re-entrant super().__init__ call initializes instead of being skipped.
_thread_state = threading.local()


def _initializing_classes() -> set[type]:
    classes = getattr(_thread_state, "classes", None)
    if classes is None:
        classes = set()
        _thread_state.classes = classes
    return classes


def _initialize_once(cls: type, initialize: Callable[[], None]) -> None:
    """Run ``initialize`` for ``cls`` the first time, and only then.

    A re-entrant call for a class already initializing on this thread runs
    ``initialize`` directly — that is the ``super().__init__`` hop, not a second
    construction. ``setattr`` happens outside the ``try`` so a raising
    ``__init__`` leaves the class uninitialized and retryable.
    """
    if cls in _initializing_classes():
        initialize()
        return
    with _LOCK:
        if cls.__dict__.get(_INITIALIZED_ATTR):
            return
        _initializing_classes().add(cls)
        try:
            initialize()
        finally:
            _initializing_classes().discard(cls)
        setattr(cls, _INITIALIZED_ATTR, True)


def _guard_init(original: Callable[..., None]) -> Callable[..., None]:
    """Wrap a class's own ``__init__`` so it runs at most once per class."""

    def guarded(self: Any, *args: Any, **kwargs: Any) -> None:
        _initialize_once(type(self), lambda: original(self, *args, **kwargs))

    setattr(guarded, _GUARDED_ATTR, True)
    return guarded


class Singleton:
    """Mixin that makes each concrete subclass a process-wide singleton.

    Put it first in the bases so its ``__new__`` / ``__init__`` win the MRO::

        class AllFilter(Singleton, SearchFilter[T]): ...

    Works with plain classes, Pydantic ``BaseModel`` subclasses, and
    ``DiscriminatedUnionMixin`` hierarchies.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        instance = cls.__dict__.get(_INSTANCE_ATTR)
        if instance is not None:
            return cast(Self, instance)
        with _LOCK:
            instance = cls.__dict__.get(_INSTANCE_ATTR)
            if instance is None:
                instance = super().__new__(cls)
                setattr(cls, _INSTANCE_ATTR, instance)
            return cast(Self, instance)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        own_init = cls.__dict__.get("__init__")
        if own_init is not None and not getattr(own_init, _GUARDED_ATTR, False):
            cls.__init__ = _guard_init(own_init)  # type: ignore[method-assign]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _initialize_once(type(self), lambda: super(Singleton, self).__init__(*args, **kwargs))

    @classmethod
    def clear_singleton_cache(cls) -> None:
        """Drop *this class's* cached instance so the next build is fresh.

        Intended for tests that need a clean build between cases; not for
        runtime use. Only the class it is called on is cleared — a base and its
        subclass cache independently.
        """
        for attr in (_INSTANCE_ATTR, _INITIALIZED_ATTR):
            if attr in cls.__dict__:
                delattr(cls, attr)
