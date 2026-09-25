"""Base class for typed configuration objects.

:class:`BaseConfig` is a Pydantic v2 ``BaseModel`` subclass that every
framework / app config extends. It wires together the vendored
:func:`~resourcey.v2.util.env_parser.from_env` /
:func:`~resourcey.v2.util.env_parser.to_env` machinery with a cached
:meth:`get_instance` entry point and a
:class:`~resourcey.v2.config.lazy_field.LazyField`-aware ``model_config``.

``get_instance`` caches **per class**: ``MyAppConfig.get_instance()`` returns a
``MyAppConfig`` and ``FrameworkConfig.get_instance()`` returns a
``FrameworkConfig``. There is no super/subclass acceptance check — an app may
extend a framework config and both remain independently resolvable, so a
specific app config never needs to be known to the outer layers that only read
the framework slice.

One prefix, process-wide
------------------------
Every :class:`BaseConfig` subclass parses from the *same* env prefix, owned by
this module rather than by each class: :func:`get_config_prefix` reads it (the
default is ``APP``) and :func:`set_config_prefix` sets it. The first read
**latches**, so a later :func:`set_config_prefix` raises
:class:`~resourcey.v2.core.errors.ResourceyConfigError` — a late set would
otherwise silently reuse configs already cached under the old prefix.
:func:`set_config_prefix` clears every class's instance cache for the same
reason; :func:`_reset_config_prefix` is the test-only reset.

Because all subclasses share one flat namespace, two classes declaring the same
field name with *different* types make the env var ambiguous. That is rejected
at class-creation time by :meth:`BaseConfig.__init_subclass__` (a
:class:`TypeError`, the same point and style as ``DTO``'s id-field check) so a
collision fails at import rather than reading a value it cannot validate.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar, TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from resourcey.v2.config.lazy_field import LazyField
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.util.env_parser import from_env, to_env

# Class-level cache attribute name. Non-annotated so Pydantic ignores it;
# stored on each subclass's own ``__dict__`` so subclasses don't share caches.
_INSTANCE_CACHE_ATTR = "_cached_instance"

# The single app-wide env prefix and the one-way "already read" latch. A set
# after the first read would silently reuse configs cached under the old prefix,
# so it is rejected rather than merely documented.
_config_prefix: str = "APP"
_config_prefix_retrieved: bool = False

# Every BaseConfig subclass, so ``set_config_prefix`` can drop stale instances
# without walking the (deep, partly abstract) subclass tree.
_config_subclasses: list[type[BaseConfig]] = []

# ``field_name -> (resolved_type, declaring_class_name)`` across every subclass.
# A name redeclared with a different type is ambiguous in the flat namespace.
_field_types: dict[str, tuple[Any, str]] = {}

TConfig = TypeVar("TConfig", bound="BaseConfig")


def get_config_prefix() -> str:
    """The process-wide env prefix, latching on first read.

    The latch makes "set the prefix before the first build" enforced rather
    than merely documented: after this returns, :func:`set_config_prefix`
    raises.
    """
    global _config_prefix_retrieved
    _config_prefix_retrieved = True
    return _config_prefix


def set_config_prefix(prefix: str) -> None:
    """Set the process-wide prefix, before any config has been read.

    Raises :class:`~resourcey.v2.core.errors.ResourceyConfigError` once
    :func:`get_config_prefix` has been called, and clears every subclass's
    cached instance so none survives under the old prefix.
    """
    global _config_prefix
    if _config_prefix_retrieved:
        raise ResourceyConfigError(
            "The config prefix has already been read; set it before the first "
            "config is built (get_config_prefix() latches on first read)."
        )
    _config_prefix = prefix
    for subclass in _config_subclasses:
        subclass.clear_instance_cache()


def _reset_config_prefix(prefix: str = "APP") -> None:
    """Test-only: restore ``prefix`` and clear the latch and all caches."""
    global _config_prefix, _config_prefix_retrieved
    _config_prefix = prefix
    _config_prefix_retrieved = False
    for subclass in _config_subclasses:
        subclass.clear_instance_cache()


class BaseConfig(BaseModel):
    """Base class for typed, env-driven configuration.

    Subclass it and declare Pydantic fields; the instance is built from
    environment variables via :meth:`get_instance`, which caches the result on
    the class it was called on and parses under the process-wide prefix from
    :func:`get_config_prefix`.
    """

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        ignored_types=(LazyField,),  # keep LazyField as a class attribute, not a field
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _register_field_types(cls)
        _config_subclasses.append(cls)

    @classmethod
    def get_prefix(cls) -> str:
        """Return the process-wide env-var prefix (default ``APP``)."""
        return get_config_prefix()

    @classmethod
    def get_instance(cls: type[TConfig]) -> TConfig:
        """Build (and cache) this class's config from env vars.

        Reads :data:`os.environ` under the process-wide prefix; the result is
        cached on the class, so repeated calls return the same instance — and a
        subclass keeps its own cache independent of its base. ``v2`` does no
        ``.env`` loading of its own (use ``uvicorn --env-file`` or the like).
        """
        cached = cls.__dict__.get(_INSTANCE_CACHE_ATTR)
        if cached is not None:
            return cast(TConfig, cached)
        try:
            instance = cast(TConfig, from_env(cls, prefix=cls.get_prefix()))
        except (ValidationError, ValueError, TypeError) as exc:
            raise ResourceyConfigError(
                f"Failed to build {cls.__name__} from environment (prefix "
                f"'{cls.get_prefix()}'): {exc}"
            ) from exc
        setattr(cls, _INSTANCE_CACHE_ATTR, instance)
        return instance

    def generate_env_template(self, *, prefix: str | None = None) -> str:
        """Serialize this instance to a commented, valued ``.env`` template.

        Field descriptions appear as comments. Typically called on a
        defaults-only instance (``MyAppConfig().generate_env_template()``) or
        on the cached instance
        (``MyAppConfig.get_instance().generate_env_template()``).
        """
        return to_env(self, prefix=prefix or type(self).get_prefix())

    @classmethod
    def clear_instance_cache(cls) -> None:
        """Drop *this class's* cached instance so the next build is fresh.

        Intended for tests that flip env vars between cases; not for runtime
        use. Only the class it is called on is cleared — a base and its
        subclass cache independently, so clearing one leaves the other alone.
        """
        if _INSTANCE_CACHE_ATTR in cls.__dict__:
            delattr(cls, _INSTANCE_CACHE_ATTR)


def _register_field_types(cls: type[BaseConfig]) -> None:
    """Register a subclass's own fields, rejecting an ambiguous redeclaration.

    ``cls.model_fields`` is not populated when ``__init_subclass__`` runs, so the
    annotations are read from the class's own ``__dict__`` via
    :func:`inspect.get_annotations` (inherited fields belong to the base) and
    resolved eagerly. ``ClassVar`` entries (``LazyField``) are not fields and are
    skipped. Only the declaring class's own fields are registered, and a
    same-name/same-type redeclaration across classes is allowed — it denotes the
    same env var.
    """
    own_annotations = inspect.get_annotations(cls, eval_str=True)
    if not own_annotations:
        return
    for name, annotation in own_annotations.items():
        if getattr(annotation, "__origin__", None) is ClassVar:
            continue
        existing = _field_types.get(name)
        if existing is not None and existing[0] is not annotation:
            raise TypeError(
                f"Config field {name!r} is declared with conflicting types: "
                f"{existing[1]} declares {existing[0]!r}, {cls.__name__} declares "
                f"{annotation!r}. All config classes share one env prefix, so a "
                "shared field name must share its type."
            )
        _field_types[name] = (annotation, cls.__name__)
