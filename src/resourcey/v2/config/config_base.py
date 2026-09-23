"""Base class for typed configuration objects.

:class:`BaseConfig` is a Pydantic v2 ``BaseModel`` subclass that every
framework / app config extends. It wires together the vendored
:func:`~resourcey.v2.util.env_parser.from_env` /
:func:`~resourcey.v2.util.env_parser.to_env` machinery with a cached
:meth:`get_instance` entry point that folds ``.env`` file loading into the
build, and a :class:`~resourcey.v2.config.lazy_field.LazyField`-aware
``model_config``.

``get_instance`` caches **per class**: ``MyAppConfig.get_instance()`` returns a
``MyAppConfig`` and ``FrameworkConfig.get_instance()`` returns a
``FrameworkConfig``, each built from its own env prefix. There is no
super/subclass acceptance check — an app may extend a framework config and both
remain independently resolvable, so a specific app config never needs to be
known to the outer layers that only read the framework slice.
"""

from __future__ import annotations

from typing import TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from resourcey.v2.config.config_loader import load_dotenv
from resourcey.v2.config.lazy_field import LazyField
from resourcey.v2.core.errors import ResourceyConfigError
from resourcey.v2.util.env_parser import from_env, to_env

# Class-level cache attribute name. Non-annotated so Pydantic ignores it;
# stored on each subclass's own ``__dict__`` so subclasses don't share caches
# (same convention as ``BaseResource``).
_INSTANCE_CACHE_ATTR = "_cached_instance"

TConfig = TypeVar("TConfig", bound="BaseConfig")


class BaseConfig(BaseModel):
    """Base class for typed, env-driven configuration.

    Subclass it and declare Pydantic fields; the instance is built from
    environment variables (and an optional ``.env`` file) via
    :meth:`get_instance`, which caches the result on the class it was called on.
    """

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        ignored_types=(LazyField,),  # keep LazyField as a class attribute, not a field
    )

    @classmethod
    def get_prefix(cls) -> str:
        """Return the env-var prefix (default: top-level module name, upper-cased).

        e.g. a class defined in ``foo.bar.FooBarConfig`` → prefix ``FOO``.
        Overridable by subclasses that want a different prefix.
        """
        return cls.__module__.split(".")[0].upper()

    @classmethod
    def get_instance(cls: type[TConfig]) -> TConfig:
        """Build (and cache) this class's config from env vars + ``.env`` file.

        Calls :func:`load_dotenv` (default ``.env``, overridable via
        ``RESOURCEY_ENV_FILE``) so file values are in :data:`os.environ`
        first, then :func:`from_env` under this class's :meth:`get_prefix`.
        The result is cached on the class, so repeated calls return the same
        instance — and a subclass keeps its own cache independent of its base.
        """
        cached = cls.__dict__.get(_INSTANCE_CACHE_ATTR)
        if cached is not None:
            return cast(TConfig, cached)
        try:
            load_dotenv()
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
