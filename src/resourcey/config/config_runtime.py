"""Runtime selection of the active config instance (issue #11).

``get_config()`` is the single runtime entry point for reading config — internal
framework code calls it instead of hardcoding
``FrameworkConfig.get_instance()``. An app that subclasses
:class:`~resourcey.config.config_framework.FrameworkConfig` (e.g.
``myapp.config.AppConfig``) points the framework at its class via the
``RESOURCEY_CONFIG_CLASS`` env var (design A, the declarative default), or hands
a fully-built instance to :func:`set_config` (design B, the explicit escape
hatch). Resolution order:

1. Instance set via :func:`set_config` — returned verbatim.
2. ``RESOURCEY_CONFIG_CLASS`` env var — lazily imported on the first
   :func:`get_config` call, then delegated to via ``ThatClass.get_instance()``;
   falls back to ``FrameworkConfig`` when unset.
3. ``FrameworkConfig.get_instance()`` default.

Config must not be read at module-import time; call :func:`get_config` at
runtime. Reading it at import time (before ``__main__`` calls :func:`set_config`)
would resolve to the default (``FrameworkConfig``) and silently mask a later
override, so defer every read to a :func:`get_config` call inside a function or
request handler.

A set-but-invalid ``RESOURCEY_CONFIG_CLASS`` (unimportable module, missing class,
or a value that is not a :class:`~resourcey.config.config_base.BaseConfig`
subclass) raises :class:`~resourcey.resource.errors.ResourceyConfigError`
naming the env var, mirroring :class:`~resourcey.config.lazy_field.LazyField`'s
error contract. An unset var is not an error — it falls back to
``FrameworkConfig``.
"""

from __future__ import annotations

import importlib
import os
from typing import TypeVar

from resourcey.config.config_base import BaseConfig
from resourcey.config.config_framework import FrameworkConfig
from resourcey.resource.errors import ResourceyConfigError

_CONFIG_CLASS_ENV = "RESOURCEY_CONFIG_CLASS"

# Module-global override set by set_config(); wins over the env-var path.
_config_override: BaseConfig | None = None
# Cached env-var-resolved instance so repeated get_config() calls return the
# same object; dropped by clear_config_cache() for test isolation.
_env_resolved_instance: BaseConfig | None = None

T = TypeVar("T", bound=BaseConfig)


def get_config() -> BaseConfig:
    """Return the active config instance.

    Resolution order: :func:`set_config` override → ``RESOURCEY_CONFIG_CLASS``
    env var → ``FrameworkConfig.get_instance()``. The env-var-resolved result is
    cached so repeated calls return the same instance; call
    :func:`clear_config_cache` to rebuild (e.g. between tests that flip env
    vars). See the module docstring for the import-time-read hazard.
    """
    if _config_override is not None:
        return _config_override
    if _env_resolved_instance is not None:
        return _env_resolved_instance
    instance = _resolve_from_env()
    _set_env_resolved(instance)
    return instance


def set_config(instance: BaseConfig) -> None:
    """Set the module-global config override (the escape hatch).

    The instance wins over the ``RESOURCEY_CONFIG_CLASS`` env-var path and is
    returned verbatim by subsequent :func:`get_config` calls. Pass a fully-built
    instance from any source (non-env, dynamic selection, test fixture).
    """
    _set_override(instance)


def get_config_as(cls: type[T]) -> T:
    """Return the active config instance typed as ``cls``.

    Raises :class:`~resourcey.resource.errors.ResourceyConfigError` if the
    resolved instance is not a ``cls``, giving apps a typed boundary without
    defeating env-var discovery.
    """
    instance = get_config()
    if not isinstance(instance, cls):
        raise ResourceyConfigError(
            f"Active config is {type(instance).__name__}, not a {cls.__name__} "
            f"(required by get_config_as({cls.__name__}))"
        )
    return instance


def clear_config_cache() -> None:
    """Drop the module-global override and any cached env-var-resolved instance.

    The next :func:`get_config` call rebuilds. Intended for test isolation (flip
    env vars between cases); mirrors ``BaseConfig.clear_instance_cache``. Not
    for runtime use.
    """
    _set_override(None)
    _set_env_resolved(None)


def _resolve_from_env() -> BaseConfig:
    """Resolve the config class from ``RESOURCEY_CONFIG_CLASS`` or fall back.

    A set-but-empty var is treated as unset (fallback). A set-but-invalid value
    (bare class name with no module, unimportable module, missing class, or a
    value that is not a :class:`BaseConfig` subclass) raises
    :class:`ResourceyConfigError` naming the env var.

    Loads ``.env`` first so ``RESOURCEY_CONFIG_CLASS`` can be set in the file
    (not only in the real environment) — otherwise the config-class indirection
    would be invisible to ``resourcey migrate`` and other CLI entry points that
    never call :func:`set_config`.
    """
    from resourcey.config.config_loader import load_dotenv

    load_dotenv()
    fqn = os.environ.get(_CONFIG_CLASS_ENV)
    if not fqn:
        return FrameworkConfig.get_instance()
    module_name, _, class_name = fqn.rpartition(".")
    if not module_name:
        raise ResourceyConfigError(
            f"Env var '{_CONFIG_CLASS_ENV}'='{fqn}' must be a fully-qualified "
            "class name (module.ClassName)"
        )
    try:
        obj = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise ResourceyConfigError(
            f"Env var '{_CONFIG_CLASS_ENV}'='{fqn}' could not be imported: {exc}"
        ) from exc
    if not (isinstance(obj, type) and issubclass(obj, BaseConfig)):
        raise ResourceyConfigError(
            f"Env var '{_CONFIG_CLASS_ENV}'='{fqn}' does not subclass BaseConfig"
        )
    return obj.get_instance()


# Setters are thin indirection so get_config / set_config / clear_config_cache
# can mutate the module globals through a single path — keeps the ``global``
# declarations co-located and the read paths free of them.
def _set_override(instance: BaseConfig | None) -> None:
    global _config_override
    _config_override = instance


def _set_env_resolved(instance: BaseConfig | None) -> None:
    global _env_resolved_instance
    _env_resolved_instance = instance
