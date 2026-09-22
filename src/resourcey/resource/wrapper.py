"""``WrapperResourceBase`` — composition-based resource customisation.

A wrapper holds a reference to an inner :class:`~resourcey.resource.base.BaseResource`
and delegates every hook to it by default. A subclass (or a configured
instance) overrides only the hooks it needs to change — the rest pass through
unchanged. This is the composition pattern of choice for customisation:

* **Subtract attributes from the read model** — override ``get_read_model``
  to produce a model that excludes sensitive fields.
* **Narrow exposed actions** — override ``get_supported_actions`` to hide
  an action the inner resource supports but should not be public.
* **Change REST presence** — override ``get_exposed_resource`` on the *inner*
  resource to return a public wrapper (issue #62). A wrapper always reports
  itself as exposed; it never delegates that decision to the inner.

The wrapper does **not** declare its own fields (``__init_subclass__`` is a
no-op); it proxies ``model_fields`` and all generation hooks to the inner
resource. A wrapper is never a storage backend — it does not build its own
SQLAlchemy model or open its own sessions. It delegates ``open_storage`` to
the inner resource (so it reuses the same session/collection) and
``build_service`` to the inner resource *bound to the wrapper*, so the service
layer reads/writes the inner table while shaping the response through the
wrapper's read model.

Example — an internal ``User`` whose outside-world view hides ``password`` and
``idp_user_id`` (issue #62)::

    class User(SqlResource):
        id: int
        name: str
        password: str

        def get_exposed_resource(self) -> BaseResource | None:
            return PublicUser(inner=self)

    class PublicUser(WrapperResourceBase):
        _exclude_read: frozenset[str] = frozenset({"password", "idp_user_id"})

        def get_read_model(self) -> type[BaseModel]:
            return self._project_read_model(exclude=self._exclude_read)

``User`` is internal-only and ``GET /public-users/{id}`` comes from
``PublicUser``, so ``password`` never appears in a response body. Wiring a
plain resource is just ``class Widget(SqlResource)`` with no override — the
default ``get_exposed_resource`` returns ``self``.
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

    # NOTE: the wrapper deliberately does **not** override
    # ``get_exposed_resource``. The inherited default returns ``self`` (the
    # wrapper), which is the resource the outside world sees. Delegating to
    # ``self._inner.get_exposed_resource()`` would be a mistake: an inner that
    # does not override the hook returns *itself*, so the route builder would
    # receive the inner resource and discard the wrapper's projection — a
    # data-leak hazard (the hidden field returns). Returning ``self`` is also
    # non-recursive and idempotent, so it needs no fixed-point rule.

    # ------------------------------------------------------------------
    # Service + action surface — delegate to inner
    # ------------------------------------------------------------------

    @property
    def actions(self) -> frozenset[Any]:
        return self._inner.actions

    def get_supported_actions(self) -> frozenset[Any]:
        return self._inner.get_supported_actions()

    def get_orm_model(self) -> Any:
        """The ORM model of the inner resource (issue #62).

        A wrapper is not a storage backend: its table is the inner resource's,
        so a service built on the wrapper reads/writes the inner table while
        shaping the response through the wrapper's read model.
        """
        return self._inner.get_orm_model()

    def migrate_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Delegate lazy document migration to the inner resource."""
        return self._inner.migrate_document(doc)

    def open_storage(self, request: Any) -> Any:
        return self._inner.open_storage(request)

    def build_service(self, resource: BaseResource, storage: Any) -> Any:
        """Ask the inner resource for a service bound to *this* wrapper.

        ``resource`` is ``self`` (the wrapper) — the inner resource builds its
        service over the wrapper so the service's read model is the wrapper's
        projection, which is what makes field hiding apply to the response body
        and not merely the OpenAPI schema.
        """
        return self._inner.build_service(resource, storage)

    # ``get_service_dependency`` is inherited: BaseResource's default composes
    # ``open_storage`` + ``build_service``, both of which delegate above (to the
    # inner storage, and to the inner resource bound to this wrapper).

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
