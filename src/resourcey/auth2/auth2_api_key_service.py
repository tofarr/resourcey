"""The service backing the :class:`~resourcey.auth2.auth2_api_key_resource.ApiKey`
resource (issue #63).

:class:`ApiKeyService` changes exactly one action: ``create`` mints the key and
returns it. Every other action is inherited from
:class:`~resourcey.resource.service.SqlService`, so a key's secret is
structurally absent from read, search, and batch responses — the read model has
no ``key`` field to populate.

The one-time reveal needs two models the resource's own generation does not
produce, because the key is neither creatable nor readable:

* the **insert** model — the create model plus ``key``, so the minted value
  reaches the repository (a client cannot supply one: the field is absent from
  the create model FastAPI validates the request body against);
* the **created** model — the read model plus ``key``, the shape of the ``201``
  response, and the only response in which a key ever appears.

Both are derived from the resource's generated models rather than declared
separately, so a field added to the resource flows into them automatically.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, create_model
from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth2.auth2_api_key_resource import KEY_FIELD, generate_api_key
from resourcey.resource.base import BaseResource
from resourcey.resource.repository import ResourceRepository
from resourcey.resource.service import SqlService


def build_insert_model(resource: BaseResource) -> type[BaseModel]:
    """The create model plus the required ``key`` the service mints."""
    return _extend_with_key(resource.get_create_model(), "Insert")


def build_created_model(resource: BaseResource) -> type[BaseModel]:
    """The read model plus ``key`` — the shape of the create response."""
    return _extend_with_key(resource.get_read_model(), "Created")


def _extend_with_key(base: type[BaseModel], suffix: str) -> type[BaseModel]:
    """``base`` with a required ``key`` field appended.

    Subclassing (rather than rebuilding the field set) keeps whatever
    validators and serializers the generated model carries.
    """
    # mypy cannot match ``create_model``'s overloads through a dynamic field
    # mapping, so the call is deliberately untyped (as in ``resource.wrapper``).
    create: Any = create_model
    model: type[BaseModel] = create(
        f"{base.__name__}{suffix}",
        __base__=base,
        **{KEY_FIELD: (str, ...)},
    )
    return model


class ApiKeyService(SqlService):
    """A :class:`SqlService` whose ``create`` mints and returns the key."""

    def __init__(
        self,
        resource: BaseResource,
        *,
        session: AsyncSession,
        repository_cls: type[ResourceRepository] | None = None,
        serialization_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            resource,
            session=session,
            repository_cls=repository_cls,
            serialization_context=serialization_context,
        )
        self.insert_model = build_insert_model(resource)
        self.created_model = build_created_model(resource)

    async def create(self, payload: BaseModel) -> Any:
        """Mint a key, persist it, and return it with the new row (HTTP 201).

        This response is the only one that carries the key, so a caller that
        loses it must mint a replacement — the value cannot be read back.
        """
        key = generate_api_key()
        created = await self.repository.insert(
            self._session, self._with_key(payload, key), context=self._ctx()
        )
        return self._reveal_key(created, key)

    def _with_key(self, payload: BaseModel, key: str) -> BaseModel:
        """The client's create payload plus the minted ``key``, for insertion."""
        supplied = payload.model_dump(exclude_unset=True)
        supplied.pop(KEY_FIELD, None)
        return self.insert_model(**supplied, **{KEY_FIELD: key})

    def _reveal_key(self, created: Any, key: str) -> BaseModel:
        """The persisted read model widened with the key that was just minted."""
        fields = {name: getattr(created, name) for name in type(created).model_fields}
        fields[KEY_FIELD] = key
        return self.created_model(**fields)
