"""Lazy polymorphic config fields resolved on first access.

A :class:`LazyField` descriptor lets a :class:`~resourcey.config.config_base.BaseConfig`
declare a field typed as an abstract :class:`~resourcey.util.models.DiscriminatedUnionMixin`
base whose concrete subclass is **not loaded** at config-build time. The
subclass is chosen by a ``{NAME}_CLASS`` env var and imported lazily on the
first attribute access — after the config is fully built, so the concrete
package and the config can reference each other without a circular import.

This complements (does not replace) the env parser's eager
:class:`~resourcey.util.env_parser.DiscriminatedUnionEnvParser`; the existing
discriminated-union path is left intact.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, ClassVar, cast, get_type_hints

from pydantic import ValidationError

from resourcey.resource.errors import ResourceyConfigError
from resourcey.util.env_parser import from_env


class LazyField:
    """Descriptor that lazily resolves a polymorphic config field on first access.

    The field's name (→ env-var prefix) and base type (→ subclass check) are
    inferred from the class declaration via :meth:`__set_name__` and the
    ``ClassVar`` annotation, so no arguments are needed.

    Usage::

        class AppConfig(BaseConfig):
            my_field: ClassVar[MyPolymorphicObj] = LazyField()
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self._owner = owner
        self._name = name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        cache_attr = f"_lazy_{self._name}"
        if cache_attr in instance.__dict__:
            return instance.__dict__[cache_attr]
        value = self._resolve()
        instance.__dict__[cache_attr] = value
        return value

    def _resolve(self) -> Any:
        base = _unwrap_classvar(get_type_hints(self._owner)[self._name])
        prefix = self._name.upper()
        class_var = f"{prefix}_CLASS"
        fqn = os.environ.get(class_var)
        if not fqn:
            raise ResourceyConfigError(
                f"Missing env var '{class_var}' for lazy field '{self._name}' "
                f"on {self._owner.__name__}"
            )
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
