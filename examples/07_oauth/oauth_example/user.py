"""The ``User`` resource — the local principal store, served read-only.

The ORM model (in :mod:`oauth_example.models`) is the schema of record, so
:class:`~resourcey.sql.sql_resource.SqlResource` infers the DTO and REST models
from it. The resource is wrapped in a
:class:`~resourcey.view.resource_view.ResourceView` that narrows it to the read
subset, so no write route is ever mounted — a ``User`` row is created by the seed
script / migration, not through the public API.

This is the store the ``OAuthAuthenticator`` validates a resolved external
identity against (``user_resource=``): a valid IdP token whose internal user is
missing or ``enabled=False`` is rejected. The authenticator holds the **inner**
resource; the manifest registers the **view**.
"""

from __future__ import annotations

from typing import Any

from oauth_example.models import User
from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_resource import SqlResource
from resourcey.view.resource_view import ResourceView

# The read subset: a read-only resource advertises exactly these, so the
# transport mounts no write route and a write is a 405 rather than a handler.
USER_EXPOSED_ACTIONS = frozenset({Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ})


def user_resource(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
) -> Resource[Any, Any]:
    """The **inner** ``User`` resource: the ``users`` table, read-only by view."""
    return SqlResource(
        User,
        session_manager=session_manager,
        session_factory=session_factory,
        path="users",
    )


def user_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed ``User`` view: read-only, so no write route exists."""
    return ResourceView(inner, exposed_actions=USER_EXPOSED_ACTIONS)
