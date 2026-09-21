"""``MongoResource`` — the MongoDB-backed resource declaration layer.

:class:`~resourcey.resource.base.BaseResource` is storage-agnostic: it handles
field collection and the generated Pydantic create / read / update models.
``MongoResource`` extends it with the MongoDB concerns: the collection name,
an async ``motor`` client/factory held as instance state, and the per-request
:meth:`open_service` that yields a :class:`~resourcey.mongo.mongo_service.MongoService`
bound to a collection.

A non-SQL resource does **not** participate in Alembic migrations. Instead,
:meth:`migrate_document` is an opt-in hook (default no-op) invoked on read so
an application can lazily upgrade a document to the current shape. The
versioning scheme is application-defined — most implementations carry a
schema-version field, but the framework does not prescribe it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from resourcey.app_context import AppContext
from resourcey.resource.base import BaseResource
from resourcey.resource.errors import ResourceyConfigError
from resourcey.util.naming import camel_to_snake, pluralize

# AppContext cache key for the shared Mongo client. Resources look this up on
# the context so an escape-hatch caller can pre-seed a client and skip the
# default build.
_MONGO_CLIENT_KEY = object()


async def _noop_dispose() -> None:
    """No-op disposer for clients that need no explicit close (embedded)."""


class MongoResource(BaseResource):
    """A resource declaration backed by a MongoDB collection.

    Adds the Mongo concerns on top of :class:`BaseResource`: the collection
    name, a ``motor`` client / database built by ``__aenter__`` (via
    :meth:`build_client`), and :meth:`open_service` yielding a
    :class:`~resourcey.mongo.mongo_service.MongoService`. There is no ORM
    model and no Alembic migration -- a Mongo resource stores documents
    directly and upgrades them lazily via :meth:`migrate_document`.
    """

    # Per-instance ``motor`` client / collection used by ``open_service``.
    # Set by ``__aenter__`` before the app serves requests. ``None`` means
    # unconfigured.
    _client: Any = None
    _database_name: str = "resourcey"
    _db: Any = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _make_id_optional(cls)

    # ------------------------------------------------------------------
    # Registration hook
    # ------------------------------------------------------------------

    def on_register(self) -> None:
        """No eager materialisation needed for a Mongo resource.

        Unlike ``SqlResource`` (which builds its ORM model), a Mongo resource
        has no schema to materialise. Indexes are created in ``__aenter__``
        via :meth:`ensure_indexes`.
        """

    # ------------------------------------------------------------------
    # Service + client configuration
    # ------------------------------------------------------------------

    @classmethod
    def get_service_cls(cls) -> type[Any]:
        from resourcey.mongo.mongo_service import MongoService

        return MongoService

    async def __aenter__(self, ctx: AppContext) -> AppContext:
        """Build (or reuse) the shared Mongo client, run indexes, then enter.

        If no client is cached on this instance, build one via
        :meth:`build_client`. The client's disposer is registered on ``ctx``.
        A client pre-seeded on ``ctx`` (the escape hatch) is adopted without
        building. Then ``ensure_indexes`` runs for this resource's collection.
        """
        await super().__aenter__(ctx)
        if self._client is None:
            if ctx.has(_MONGO_CLIENT_KEY):
                self._client = ctx.get(_MONGO_CLIENT_KEY)
                self._db = self._client[self._database_name]
            else:
                client, database_name, dispose = self.build_client(ctx)
                self._client = client
                self._database_name = database_name
                self._db = client[database_name]
                ctx.set(_MONGO_CLIENT_KEY, client)
                ctx.add_disposer(dispose)
        await self.ensure_indexes()
        return ctx

    async def __aexit__(self, *exc: object) -> None:
        """Clear instance client state so a fresh manifest starts clean."""
        self._client = None
        self._db = None
        await super().__aexit__(*exc)

    def build_client(self, ctx: AppContext) -> tuple[Any, str, Any]:
        """Build the shared ``motor`` client + return its disposer.

        Default: read ``mongo.url`` / ``mongo.database`` from the app config
        (``ctx.config``). When the URL is ``embedded`` (or empty), use the
        in-process :class:`~resourcey.mongo.embedded.AsyncEmbeddedClient``. Otherwise
        build a real ``motor`` client against the URL.

        Returns:
            ``(client, database_name, disposer)`` where ``disposer`` is a
            no-arg async callable run on app shutdown (e.g. ``client.close``).
        """
        config = ctx.config
        mongo = getattr(config, "mongo", None)
        if mongo is not None:
            url = mongo.url
            database_name = mongo.database
        else:
            url = "embedded"
            database_name = "resourcey"
        url = (url or "embedded").strip()
        client: Any
        if not url or url == "embedded":
            from resourcey.mongo.embedded import AsyncEmbeddedClient

            client = AsyncEmbeddedClient()
            return client, database_name, _noop_dispose
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(url)
        return client, database_name, client.close

    def get_collection(self) -> Any:
        """The configured ``motor`` collection for this resource.

        Raises if ``__aenter__`` has not run -- a Mongo resource cannot open
        a service without a client.
        """
        if self._db is None:
            raise ResourceyConfigError(
                f"{type(self).__name__} has no Mongo client configured; "
                "the manifest lifecycle sets this via build_client."
            )
        return self._db[type(self).get_collection_name()]

    def open_service(self, request: Any) -> Any:
        """Async context manager yielding a :class:`MongoService` for ``request``.

        Yields a :class:`~resourcey.mongo.mongo_service.MongoService` bound to
        this resource's collection.
        """
        return _open_mongo_service(self)

        # ------------------------------------------------------------------

    # Collection naming
    # ------------------------------------------------------------------

    @classmethod
    def get_collection_name(cls) -> str:
        """Derive the Mongo collection name from the class name.

        Snake-case the class name (``UserRole`` -> ``user_role``), pluralize,
        and lowercase — mirroring :meth:`SqlResource.get_table_name` so the
        collection name and the URL path stay independent concerns.
        """
        return pluralize(camel_to_snake(cls.__name__)).lower()

    # ------------------------------------------------------------------
    # Id convention (client-generated, not auto-increment)
    # ------------------------------------------------------------------

    @classmethod
    def get_config_for_field(cls, field_name: str, field: Any) -> Any:
        """Make the ``id`` field creatable so a client supplies it on create.

        Unlike SQL (where ``id`` is auto-incremented by the DB and therefore
        not creatable), a Mongo document's ``_id`` is client-generated. The
        framework defaults the id to a fresh ``UUID`` when the client omits it
        (see :meth:`MongoService._payload_to_doc`), so the id stays creatable
        but optional. The id remains non-updatable (``_id`` is immutable in
        Mongo).
        """
        config = super().get_config_for_field(field_name, field)
        if field_name == "id":
            config = config.model_copy(update={"creatable": True})
        return config

    # ------------------------------------------------------------------
    # Indexes (opt-in, application-driven — no DDL / migration step)
    # ------------------------------------------------------------------

    async def ensure_indexes(self) -> None:
        """Create indexes declared by :meth:`get_indexes`.

        Called by the manifest lifecycle at startup (or manually) since there
        is no migration step. Override :meth:`get_indexes` to declare index
        specs; the default returns an empty list (no indexes beyond ``_id``).
        """
        indexes = self.get_indexes()
        if not indexes:
            return
        collection = self.get_collection()
        for spec in indexes:
            await collection.create_index(
                spec["key"], name=spec.get("name"), **spec.get("options", {})
            )

    def get_indexes(self) -> list[dict[str, Any]]:
        """Index specifications for :meth:`ensure_indexes`.

        Each spec is ``{"key": [(field, direction)], "name": str|None,
        "options": dict}``. The default is empty (only the ``_id`` index
        exists). Override to declare secondary indexes.
        """
        return []

    # ------------------------------------------------------------------
    # Manual migration on read (opt-in, application-defined)
    # ------------------------------------------------------------------

    def migrate_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Lazily upgrade a document to the current shape on read (default no-op).

        Invoked by :meth:`MongoService._doc_to_read_model` before projecting
        a document into the read model. The default returns the document
        unchanged. An application overrides this to coordinate schema upgrades
        — most implementations carry a schema-version number on each document
        and upgrade in place, but the framework does not prescribe the
        versioning scheme, the upgrade function signatures, or the storage of
        the version field. Returning a new dict (rather than mutating) is
        safe and keeps the stored document untouched unless the override
        writes back.
        """
        return doc


@asynccontextmanager
async def _open_mongo_service(resource: MongoResource) -> Any:
    """Yield a :class:`MongoService` bound to the resource's collection.

    Motor manages its own connection pool, so unlike the SQL path there is no
    per-request session to commit/close — the service reads/writes the
    collection directly and each operation is atomic at the document level.
    """
    from resourcey.mongo.mongo_service import MongoService

    collection = resource.get_collection()
    yield MongoService(resource, collection=collection)


def _make_id_optional(cls: type[MongoResource]) -> None:
    """Give the id field a default so it is optional in the create model.

    Mongo documents use client-generated ids (a fresh ``UUID`` when the client
    omits it), not DB auto-increment. The framework's
    :meth:`MongoService._payload_to_doc` supplies a ``uuid4()`` when the create
    payload lacks an id, so the create model must treat the id as optional.
    Replaces the id field's ``FieldInfo`` with one carrying ``default=None``
    so ``is_required()`` returns ``False``.
    """
    from pydantic.fields import FieldInfo

    id_field = "id"
    if id_field not in cls.model_fields:
        return
    field = cls.model_fields[id_field]
    if not field.is_required():
        return
    cls.model_fields[id_field] = FieldInfo(
        annotation=field.annotation,
        default=None,
        description=field.description,
    )
