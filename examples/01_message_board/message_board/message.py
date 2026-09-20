"""The ``Message`` resource — the child side of the message board.

A message belongs to a single ``Thread`` via ``thread_id``, a real foreign-key
column to ``threads.id``. Until issue #24 ships a higher-level relation API,
the FK is expressed with the explicit ``ResourceyField(column=...)`` escape
hatch so the example is unblocked today and becomes the integration test that
#24's relation API must keep satisfying.

A ``MessageSearchFilter`` declares ``thread_id__eq`` so a client can list a
thread's messages via ``GET /messages?thread_id__eq=<id>``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from resourcey.resource.field import ResourceyField
from resourcey.resource.sql import SqlResource
from resourcey.util.search_filter import BaseSearchFilter
from sqlalchemy import Column, ForeignKey, Integer


class Message(SqlResource):
    """A message belonging to a thread.

    Fields:
        id: Auto-incrementing primary key.
        thread_id: FK to ``threads.id`` (one-to-many). Required, not creatable
            via the framework's id convention but explicitly creatable here
            (a client must supply it on create).
        text: The message body (required).
        created_at: Set automatically on create; never creatable/updatable.
        updated_at: Refreshed automatically on update; never creatable/updatable.
    """

    id: int
    thread_id: Annotated[
        int,
        ResourceyField(
            column=Column(
                "thread_id", Integer, ForeignKey("threads.id"), nullable=False, index=True
            )
        ),
    ]
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def get_search_filter_type(cls) -> type[MessageSearchFilter]:
        """Expose ``thread_id__eq`` filtering so a thread's messages can be listed."""
        return MessageSearchFilter


# Eagerly build the ORM model so the table is registered in metadata before
# migrations or table creation run, and so ``MessageSearchFilter`` can be
# parameterized with the materialised model below.
_MessageModel = Message.get_sql_alchemy_model()


# Parameterizing with the generated SQLAlchemy model lets ``BaseSearchFilter``
# find the ``thread_id`` column on it for SQL WHERE clauses. The model is a
# dynamically-generated class (``type(...)``), so ``_MessageModel`` is not a
# static type alias — the ``type: ignore`` silences mypy on the subscription.
class MessageSearchFilter(BaseSearchFilter[_MessageModel]):  # type: ignore[valid-type]
    """Optional filter clauses for ``Message.search``.

    Declares ``thread_id__eq`` so ``GET /messages?thread_id__eq=<id>`` lists a
    thread's messages. ``text__contains`` supports substring search on the body.
    """

    thread_id__eq: int | None = None
    text__contains: str | None = None
