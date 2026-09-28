"""The ``User`` resource — the stored principal, served read-only.

The ORM model (in :mod:`simple_roles.models`) is the schema of record, so
:class:`~resourcey.sql.sql_resource.SqlResource` infers the DTO and REST
models from it. The resource is wrapped in a
:class:`~resourcey.view.resource_view.ResourceView` that narrows it to the
read subset, so no write route is ever mounted — a ``User`` row is created by
the seed script / migration, not through the public API.

Authorization is the app's role rules, not this module: the resolver maps no
role to a ``User`` grant and deliberately omits it from the public read defaults,
so only ``ADMIN`` (``AllowAll``) may read the collection. A ``USER`` /
``MODERATOR`` / anonymous caller searching ``/users`` gets an empty page and a
by-id read is a 404 — existence is not leaked — because the
:class:`~resourcey.auth.auth_authorized_service.AuthorizedService` empties a
denied collection and hides a denied row.

The authenticator holds the **inner** resource (to validate a key's principal
against it); the manifest registers the **view**.
"""

from __future__ import annotations

from typing import Any

from resourcey.core.resource import Resource
from resourcey.core.service import Action
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_resource import SqlResource
from resourcey.view.resource_view import ResourceView
from simple_roles.models import User

# The read subset: a read-only resource advertises exactly these, so the
# transport mounts no write route and a write is a 405 rather than a handler.
USER_EXPOSED_ACTIONS = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


def user_resource(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
) -> Resource[Any, Any]:
    """The **inner** ``User`` resource: the ``users`` table, read-only by view.

    The authenticator holds this (it reads a principal by id); register
    :func:`user_view` over it, not this.
    """
    return SqlResource(
        User,
        session_manager=session_manager,
        session_factory=session_factory,
        path="users",
    )


def user_view(inner: Resource[Any, Any]) -> ResourceView[Any, Any]:
    """The exposed ``User`` view: read-only, so no write route exists."""
    return ResourceView(inner, exposed_actions=USER_EXPOSED_ACTIONS)
