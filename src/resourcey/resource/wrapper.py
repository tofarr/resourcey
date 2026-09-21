"""``WrapperResourceBase`` — composition-based resource customisation.

A wrapper holds a reference to an inner :class:`~resourcey.resource.base.BaseResource`
and delegates every hook to it by default. A subclass (or a configured
instance) overrides only the hooks it needs to change — the rest pass through
unchanged. This is the composition pattern of choice for customisation:

* **Subtract attributes from the read model** — override ``get_read_model``
  to produce a model that excludes sensitive fields.
* **Narrow exposed actions** — override ``get_supported_actions`` to hide
  an action the inner resource supports but should not be public.
* **Change REST presence** — override ``is_exposed`` (default ``True``; an
  inner resource with ``is_exposed=False`` stays internal while a wrapper
  exposes a public variant).

The wrapper does **not** declare its own fields (``__init_subclass__`` is a
no-op); it proxies ``model_fields`` and all generation hooks to the inner
resource. A wrapper is never a storage backend — it does not build its own
SQLAlchemy model or open its own sessions. It delegates ``open_service`` to
the inner resource so the service layer sees the inner resource's models and
tables, but the read model the client receives is the wrapper's (shaped)
variant.

Example — a public variant of ``User`` that hides ``password`` and
``idp_user_id``::

    class PublicUser(WrapperResourceBase):
        _exclude_read: frozenset[str] = frozenset({"password", "idp_user_id"})

        def get_read_model(self) -> type[BaseModel]:
            if self._read_model_cache is None:
                self._read_model_cache = _project_read_model(
                    f"{type(self).__name__}Read",
                    self._inner.model_fields,
                    self._inner,
                    exclude=self._exclude_read,
                )
            return self._read_model_cache

    public_user = PublicUser(inner=user)

The inner ``User`` resource has ``is_exposed=False`` (internal-only); the
``PublicUser`` wrapper has ``is_exposed=True`` (default) and produces a read
model that subtracts the excluded fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from resourcey.resource.base import BaseResource

if TYPE_CHECKING:
    from pydantic import BaseModel

    from resourcey.cache.cache_strategy import CacheStrategy
    from resourcey.resource.field import ResourceyField
    from resourcey.util.search_filter import SearchFilter


class WrapperResourceBase(BaseResource):
    """A proxy resource that delegates to an inner resource by default.

    Override individual hooks to change behaviour; the rest pass through.
    The wrapper does not declare its own fields or build its own storage
    artifacts — it is a *shaping* layer, not a storage layer.
    """

    # Marker: tells BaseResource.__init_subclass__ to skip field collection
    # (model_fields is proxied to the inner resource via a property).
    _is_wrapper_base: bool = True

    # Instance state (set in __init__ via object.__setattr__ to bypass
    # Pydantic's __setattr__). Declared here so mypy/static analysis can
    # resolve the types; excluded from field collection by the marker above.
    _inner: BaseResource
    _read_model_cache: type[BaseModel] | None

    def __init__(self, *, inner: BaseResource) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_read_model_cache", None)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Wrappers do not declare their own fields — skip field collection.
        # ``model_fields`` is delegated to the inner resource via the
        # property below. We do NOT call super().__init_subclass__() because
        # that would run BaseResource.__init_subclass__, which sets
        # ``cls.model_fields`` as a class attribute and shadows the property.
        pass

    @property
    def model_fields(self) -> dict[str, Any]:
        """Delegate to the inner resource's field registry."""
        return self._inner.model_fields

    @model_fields.setter
    def model_fields(self, _: Any) -> None:
        # ``BaseResource.__init_subclass__`` sets ``cls.model_fields`` on
        # subclasses, but the wrapper overrides ``__init_subclass__`` as a
        # no-op so this never runs for wrapper subclasses. The setter exists
        # so a base ``BaseResource`` subclass check doesn't fail if it's
        # called from a non-wrapper path.
        pass

    # ------------------------------------------------------------------
    # Lifecycle — delegate to inner
    # ------------------------------------------------------------------

    def on_register(self) -> None:
        self._inner.on_register()

    async def __aenter__(self, ctx: Any) -> Any:
        return await self._inner.__aenter__(ctx)

    async def __aexit__(self, *exc: object) -> None:
        await self._inner.__aexit__(*exc)

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------

    def is_exposed(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Service + action surface — delegate to inner
    # ------------------------------------------------------------------

    def get_service_cls(self) -> type[Any]:
        return self._inner.get_service_cls()

    def get_supported_actions(self) -> frozenset[Any]:
        return self._inner.get_supported_actions()

    def open_service(self, request: Any) -> Any:
        return self._inner.open_service(request)

    # ------------------------------------------------------------------
    # Field config — delegate to inner
    # ------------------------------------------------------------------

    def get_config_for_field(self, field_name: str, field: Any) -> ResourceyField:
        return self._inner.get_config_for_field(field_name, field)

    def get_search_filter_type(self) -> type[SearchFilter] | None:  # type: ignore[type-arg]
        return self._inner.get_search_filter_type()

    def get_cache_strategy(self) -> CacheStrategy[Any]:
        return self._inner.get_cache_strategy()

    def get_sortable_fields(self) -> list[str]:
        return self._inner.get_sortable_fields()

    def get_id_field(self) -> str:
        return self._inner.get_id_field()

    # ------------------------------------------------------------------
    # Pydantic model generation — delegate create/update, override read
    # ------------------------------------------------------------------

    def get_create_model(self) -> type[BaseModel]:
        return self._inner.get_create_model()

    def get_read_model(self) -> type[BaseModel]:
        return self._inner.get_read_model()

    def _project_read_model(
        self,
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> type[BaseModel]:
        """Build a read model from the inner resource's fields minus ``exclude``.

        Convenience for wrappers that subtract attributes from the inner
        resource's read model (the common case). Re-wires secret serializers
        for any ``SecretStr`` fields that remain.
        """
        from resourcey.resource.base import _project_read_model as _impl

        if self._read_model_cache is None:
            self._read_model_cache = _impl(
                f"{type(self).__name__}Read",
                self._inner.model_fields,
                self._inner,
                exclude=exclude,
            )
        return self._read_model_cache

    def get_update_model(self) -> type[BaseModel]:
        return self._inner.get_update_model()

    # ------------------------------------------------------------------
    # REST path — use wrapper's own class name
    # ------------------------------------------------------------------

    def get_resource_path(self) -> str:
        from resourcey.util.naming import camel_to_kebab, pluralize

        return pluralize(camel_to_kebab(type(self).__name__)).lower()
