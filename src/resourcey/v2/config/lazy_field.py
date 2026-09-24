"""Lazy polymorphic config fields resolved on first access.

A :class:`LazyField` descriptor lets a :class:`~resourcey.v2.config.config_base.BaseConfig`
declare a field typed as an abstract :class:`~resourcey.v2.util.models.DiscriminatedUnionMixin`
base whose concrete subclass is **not loaded** at config-build time. The
subclass is chosen by a ``{NAME}_CLASS`` env var and imported lazily on the
first attribute access — after the config is fully built, so the concrete
package and the config can reference each other without a circular import.

This complements (does not replace) the env parser's eager
:class:`~resourcey.v2.util.env_parser.DiscriminatedUnionEnvParser`; the existing
discriminated-union path is left intact.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any, ClassVar, cast, get_args, get_origin, get_type_hints

from pydantic import ValidationError

from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.util.env_parser import ListEnvParser, StrEnvParser, from_env
from resourcey.v2.util.import_paths import resolve_import_paths
from resourcey.v2.util.missing import MISSING

if TYPE_CHECKING:
    from resourcey.v2.config.config_base import BaseConfig

# Sentinel for "no default supplied" — distinct from ``None`` (a valid default).
_NO_DEFAULT: Any = object()


class LazyField:
    """Descriptor that lazily resolves a polymorphic config field on first access.

    The field's name (→ env-var prefix) and base type (→ subclass check) are
    inferred from the class declaration via :meth:`__set_name__` and the
    ``ClassVar`` annotation, so no arguments are needed.

    Usage::

        class AppConfig(BaseConfig):
            my_field: ClassVar[MyPolymorphicObj] = LazyField()

    An optional ``default`` supplies the value used when the ``{NAME}_CLASS``
    env var is **unset**, instead of raising. ``default`` may be an instance or
    a zero-arg callable returning one; each config instance gets its own call
    result (so a mutable default is not shared). The default is **not** parsed
    from the environment — it is expected to already be fully built::

        dependency_builder: ClassVar[DependencyBuilder] = LazyField(
            default=DefaultDependencyBuilder
        )

    A *set-but-empty* ``{NAME}_CLASS`` raises rather than falling back to the
    default. The fallback exists for "no posture configured"; an empty value is
    a misconfiguration, and silently downgrading to the default (which may be a
    no-authentication posture) is the wrong failure mode for a security setting.

    A ``ClassVar[list[type[SomeBase]]]`` field is supported too — it resolves
    a ``{NAME}`` / ``{NAME}_0`` / ``{NAME}_1`` list of dotted import paths to
    the named classes (lazily, on first access). See :meth:`_resolve_list`.
    """

    _owner: type[BaseConfig]
    _name: str
    _default: Any = _NO_DEFAULT

    def __init__(self, *, default: Any = _NO_DEFAULT) -> None:
        self._default = default

    def __set_name__(self, owner: type[BaseConfig], name: str) -> None:
        self._owner = owner
        self._name = name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        cache_attr = f"_lazy_{self._name}"
        if cache_attr in instance.__dict__:
            return instance.__dict__[cache_attr]
        annotation = get_type_hints(self._owner)[self._name]
        value = self._resolve_list(annotation) if _is_list_of_types(annotation) else self._resolve()
        instance.__dict__[cache_attr] = value
        return value

    def _resolve(self) -> Any:
        base = _unwrap_classvar(get_type_hints(self._owner)[self._name])
        prefix = self._name.upper()
        class_var = f"{prefix}_CLASS"
        fqn = os.environ.get(class_var)
        if fqn is None:
            if self._default is not _NO_DEFAULT:
                return self._default() if callable(self._default) else self._default
            raise ResourceyConfigError(
                f"Missing env var '{class_var}' for lazy field '{self._name}' "
                f"on {self._owner.__name__}"
            )
        # A set-but-empty value is a misconfiguration, not "unconfigured": the
        # default fallback above only covers an unset var. Falling through here
        # with "" would otherwise raise the "must be fully-qualified" error,
        # which is correct — but name the real problem.
        if not fqn.strip():
            raise ResourceyConfigError(
                f"Env var '{class_var}' is set but empty for lazy field '{self._name}' "
                f"on {self._owner.__name__}; unset it to use the default, or set a "
                "fully-qualified class name (module.ClassName)"
            )
        fqn = fqn.strip()
        module_name, _, class_name = fqn.rpartition(".")
        if not module_name:
            raise ResourceyConfigError(
                f"Env var '{class_var}'='{fqn}' for lazy field '{self._name}' must be a "
                "fully-qualified class name (module.ClassName)"
            )
        cls = getattr(importlib.import_module(module_name), class_name)
        if not (isinstance(cls, type) and issubclass(cls, base)):
            raise ResourceyConfigError(
                f"Env var '{class_var}'='{fqn}' for lazy field '{self._name}' does not "
                f"subclass the declared base {base.__name__}"
            )
        try:
            return from_env(cls, prefix=prefix)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ResourceyConfigError(
                f"Invalid configuration for lazy field '{self._name}' (prefix '{prefix}'): {exc}"
            ) from exc

    def _resolve_list(self, annotation: Any) -> list[Any]:
        """Resolve a ``list[type[Base]]`` field from dotted import paths in env.

        Reads ``{PREFIX}_{NAME}`` (JSON array) or ``{PREFIX}_{NAME}_0`` /
        ``{PREFIX}_{NAME}_1`` … and imports each path, enforcing the declared
        base via :func:`resolve_import_paths`. The prefix is the config
        class's :meth:`get_prefix` (e.g. ``RESOURCEY``) so the env var is
        consistent with the config's other fields (``RESOURCEY_MANIFEST``),
        unlike the single-class variant whose field name is the namespace for
        the imported object's own config. ``{NAME}_CLASS`` is never consulted
        — the list variant has no single class. An empty / unset list resolves
        to ``[]``.
        """
        base = _list_item_base(annotation)
        prefix = f"{self._owner.get_prefix()}_{self._name.upper()}"
        env_var = prefix
        try:
            paths = ListEnvParser(StrEnvParser(), str).from_env(env_var)
        except (ValueError, AssertionError) as exc:
            # ValueError: malformed JSON. AssertionError: ListEnvParser's
            # internal ``assert isinstance(result, list)`` when the env value
            # is valid JSON of the wrong shape (a string/object). Map both to
            # the config-error contract instead of surfacing a bare error.
            raise ResourceyConfigError(
                f"Invalid configuration for lazy field '{self._name}' (prefix '{prefix}'): "
                f"expected a JSON array of import paths — {exc}"
            ) from exc
        # ListEnvParser returns a list or MISSING (unset).
        if paths is MISSING:
            fqns: list[str] = []
        elif isinstance(paths, list):
            fqns = [p for p in paths if isinstance(p, str)]
        else:
            fqns = []
        try:
            return resolve_import_paths(fqns, base=base)
        except (ValueError, TypeError, ImportError, AttributeError) as exc:
            raise ResourceyConfigError(
                f"Invalid configuration for lazy field '{self._name}' (prefix '{prefix}'): {exc}"
            ) from exc


def _unwrap_classvar(annotation: Any) -> type:
    """Strip a ``ClassVar[...]`` wrapper so the inner type is usable with ``issubclass``.

    A subscripted ``ClassVar`` is not a valid ``issubclass`` argument; this
    helper returns the inner type. If the annotation is not a ``ClassVar`` it
    is returned unchanged.
    """
    origin = getattr(annotation, "__origin__", None)
    if origin is ClassVar:
        return cast(type, annotation.__args__[0])
    return cast(type, annotation)


def _is_list_of_types(annotation: Any) -> bool:
    """True when ``annotation`` is ``ClassVar[list[type[...]]]`` or ``list[type[...]]``.

    This is the shape of a multi-class lazy field (e.g.
    :attr:`FrameworkConfig.resources`). Strips a ``ClassVar`` wrapper first so
    the same check serves both ``ClassVar[list[...]]`` and bare ``list[...]``.
    """
    inner = annotation
    if getattr(annotation, "__origin__", None) is ClassVar:
        inner = annotation.__args__[0]
    if get_origin(inner) is not list:
        return False
    (item,) = get_args(inner)
    return get_origin(item) is type


def _list_item_base(annotation: Any) -> type:
    """The declared base of a ``list[type[Base]]`` (or ``ClassVar`` thereof).

    For ``list[type[Base]]`` returns ``Base``; for ``list[type]`` (no
    parameter) returns ``object``. Raises :class:`TypeError` if the annotation
    is not a list-of-types shape.
    """
    inner = annotation
    if getattr(annotation, "__origin__", None) is ClassVar:
        inner = annotation.__args__[0]
    if get_origin(inner) is not list:
        raise TypeError(f"Expected list[type[...]], got {annotation!r}")
    (item,) = get_args(inner)
    if get_origin(item) is not type:
        raise TypeError(f"Expected list[type[...]], got {annotation!r}")
    args = get_args(item)
    return cast(type, args[0]) if args else object
