"""The ``v2`` SQL backend: :class:`SqlResource` (issue #78, refactored #89).

The SQL workflow is model-first: a developer defines the SQLAlchemy ORM model
they already work with and hands it to :class:`SqlResource`, which infers the
DTO (and hence the REST models) from it via
:func:`~resourcey.v2.sql.sqlalchemy_2_dto.sqlalchemy_2_dto`. There is **no**
DTO-to-model generation and no declarative base to manage: SQLAlchemy is the
schema of record, so migrations and foreign-key relations stay SQLAlchemy's /
Alembic's concern, and a developer can always drop back to plain SQLAlchemy.

A column may override the inferred field projection by placing a
:class:`~resourcey.v2.core.dto.DtoField` in its ``info`` under the
``dto_field`` key.

The action layer lives in :mod:`resourcey.v2.sql.service`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import Column
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.v2.cache.cache_defaults import DefaultCacheStrategyMixin
from resourcey.v2.core.dto import RestModels
from resourcey.v2.core.resource import Resource, _camel_to_kebab, _pluralize
from resourcey.v2.core.service import Action, CacheStrategy, Service, ServiceError
from resourcey.v2.sql.filter_converter import SqlFilterContext, SqlFilterConverter
from resourcey.v2.sql.service import SqlService
from resourcey.v2.sql.sqlalchemy_2_dto import sqlalchemy_2_dto
from resourcey.v2.util.search_filter import SearchFilter, operators_for_annotation

if TYPE_CHECKING:
    from sqlalchemy import Table

    from resourcey.v2.core.manifest import Manifest
    from resourcey.v2.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)


class SqlResource(DefaultCacheStrategyMixin, Resource[T]):
    """The SQL backend: an ORM model served by :class:`SqlService`.

    Args:
        model: The SQLAlchemy declarative model to serve. Its mapped columns
            become the DTO / REST models.
        session_factory: The async session maker the service opens sessions from.
        path: An explicit REST path segment (defaults to the model name, pluralized).
        encryption_service: The service used to encrypt/decrypt pagination
            cursors; when omitted, cursors are unsupported.
    """

    def __init__(
        self,
        model: type[Any],
        *,
        session_factory: async_sessionmaker[AsyncSession],
        path: str | None = None,
        encryption_service: EncryptionService | None = None,
    ) -> None:
        self._model = model
        self._dto = sqlalchemy_2_dto(model)
        self._path = path
        self._session_factory = session_factory
        self._encryption_service = encryption_service
        self._entered = False
        self._manifest: Manifest | None = None
        # Cache policy, resolved lazily by the DefaultCacheStrategyMixin.
        self._cache_strategy: CacheStrategy | None = None
        self._column_for_attr = {
            prop.key: prop.columns[0].name for prop in model.__mapper__.column_attrs
        }
        self._attr_for_column = {col: attr for attr, col in self._column_for_attr.items()}

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The DTO model inferred from the ORM model."""
        return cast("type[T]", self._dto.get_dto_type())

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the inferred DTO's ``in_*`` flags."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name (the model's primary-key attribute)."""
        return self._dto.id_field_name

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the model name, pluralized."""
        if self._path is not None:
            return self._path.lstrip("/")
        return _pluralize(_camel_to_kebab(self._dto.__name__).lower())

    # get_cache_strategy is inherited from DefaultCacheStrategyMixin: read-only
    # → optimistic, else last-modified when ``updated_at`` is readable, else
    # ETag. Override it here to change the policy for this resource.

    def get_queryable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default query surface.

        Derived from ``read_response``, so a field projected away is not
        filterable: a wrapper that hides ``secret`` must not leave
        ``?secret__eq=`` disclosing it.
        """
        return frozenset(self.get_rest_models().read_response.model_fields)

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        """The derived filter surface: each queryable field's allowed operators.

        A field is filterable exactly when it is readable, and the operator set
        follows the field's Python type (equality always, ordering for numbers /
        datetimes, substring for strings). Override to widen or narrow it.
        """
        fields = self.get_rest_models().read_response.model_fields
        return {name: operators_for_annotation(field.annotation) for name, field in fields.items()}

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        """A declared object-filter class, or ``None`` (derive from the read model).

        Override to opt into v1-style declared filters; the class's
        ``<attribute>__<op>`` fields then define the whole surface.
        """
        return None

    # Opt-in escape hatch: when True, an unconvertible filter falls back to an
    # in-memory scan instead of raising. Off by default — see
    # ``SqlService._apply_filters``.
    allow_filter_iteration: bool = False

    # ------------------------------------------------------------------
    # Filter conversion seam
    # ------------------------------------------------------------------

    def build_filter_context(
        self, session: AsyncSession | None = None, *, allow_iteration: bool = False
    ) -> SqlFilterContext:
        """The conversion context: only *queryable* fields resolve to columns.

        Restricting ``columns`` to :meth:`get_queryable_fields` is the security
        gate: a field projected away from the read model has no column here, so
        ``?secret__eq=`` raises rather than disclosing the value. Extension
        point — a subclass may add handler inputs (dialect, resource, cache)
        without changing converter signatures.
        """
        queryable = self.get_queryable_fields()
        columns = {
            attr: self.table.c[column_name]
            for attr, column_name in self._column_for_attr.items()
            if attr in queryable
        }
        return SqlFilterContext(columns=columns, session=session, allow_iteration=allow_iteration)

    def build_filter_converter(
        self, session: AsyncSession | None = None, *, allow_iteration: bool = False
    ) -> SqlFilterConverter:
        """A converter over this resource's query surface."""
        return SqlFilterConverter(
            self.build_filter_context(session, allow_iteration=allow_iteration)
        )

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        """Every :class:`Action`; a subclass narrows by overriding."""
        return frozenset(Action)

    def get_exposed_resource(self) -> Resource[T] | None:
        """The resource the outside world sees (default: ``self``)."""
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T]:
        """Build a :class:`SqlService` over ``ctx`` and the injected session factory."""
        return SqlService(self, ctx if ctx is not None else {}, self._session_factory)

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        """Record the manifest that owns this resource."""
        self._manifest = manifest

    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this resource, or ``None``."""
        return self._manifest

    async def __aenter__(self) -> Resource[T]:
        """Enter the resource's runtime lifecycle (guards against double entry)."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the resource's runtime lifecycle."""
        self._entered = False

    # ------------------------------------------------------------------
    # Model / table surface (escape hatches to SQLAlchemy)
    # ------------------------------------------------------------------

    @property
    def model(self) -> type[Any]:
        """The ORM model this resource serves."""
        return self._model

    @property
    def table(self) -> Table:
        """The SQLAlchemy table the model maps to."""
        return cast("Table", self._model.__table__)

    @property
    def metadata(self) -> Any:
        """The metadata holding this resource's table."""
        return self.table.metadata

    @property
    def id_column(self) -> Column[Any]:
        """The table column backing the identifier.

        The DTO field name is the mapper *attribute* name, which can differ from
        the *column* name, so the identifier is resolved through the same
        attribute→column map the payload uses.
        """
        return cast("Column[Any]", self.table.c[self._column_for_attr[self.get_id_field()]])
