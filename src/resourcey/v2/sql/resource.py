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

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import Column
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resourcey.v2.cache.cache_defaults import default_cache_strategy
from resourcey.v2.core.dto import RestModels
from resourcey.v2.core.resource import Resource, _camel_to_kebab, _pluralize
from resourcey.v2.core.service import Action, CacheStrategy, Service, ServiceError
from resourcey.v2.sql.service import SqlService
from resourcey.v2.sql.sqlalchemy_2_dto import sqlalchemy_2_dto

if TYPE_CHECKING:
    from sqlalchemy import Table

    from resourcey.v2.core.manifest import Manifest
    from resourcey.v2.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)


class SqlResource(Resource[T]):
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
        # Cache policy, resolved lazily by :meth:`get_cache_strategy`.
        self._v2_cache_strategy: CacheStrategy | None = None
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

    def get_cache_strategy(self) -> CacheStrategy:
        """The default strategy: last-modified when ``updated_at`` is readable, else ETag.

        Resolved from the derived read model and cached on the *instance*, so a
        resource gets a stable strategy object across calls. (One ``SqlResource``
        class serves many models, so a class-level cache would hand one model's
        strategy to another.) A developer overrides this to change the policy
        (e.g. an ``OptimisticCacheStrategy(expire_in=60)``).
        """
        if self._v2_cache_strategy is None:
            self._v2_cache_strategy = default_cache_strategy(self.get_rest_models())
        return self._v2_cache_strategy

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
