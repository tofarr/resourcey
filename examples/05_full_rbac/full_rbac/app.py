"""Full-RBAC example app entry point (issue #133, Part 3 of the auth roadmap).

Builds on the simple-roles example (04) and replaces its **app-level, credential-
carried** role vocabulary with a **store**: real ``users`` / ``groups`` /
``group_users`` / ``roles`` / ``group_roles`` / ``role_permissions`` tables, and a
:class:`~resourcey.v2.auth.auth_rbac_resolver.RbacPolicyResolver` that resolves a
request's principal through **group → role → permission** *per request*.

The credential (an API key) carries only the principal's ``user_id``; the roles,
groups and permissions all live in the store, so a membership change takes effect
without re-issuing the credential (bounded by the resolver's optional
``cache_ttl`` / the credential's validation threshold).

The board has the same asymmetry as example 04:

* ``threads`` — a shared board: ``viewer`` reads it, ``admin`` reads/writes.
* ``messages`` — owned by ``author_id``: ``admin`` full access, ``viewer``
  read-all, ``author`` own-rows-only. A principal holding **two roles** unions
  their grants: read all of ``messages`` but update / delete only its own rows.

``RolePermission`` rows are the core RBAC unit — a ``resource`` name plus a
serialized :class:`~resourcey.v2.auth.auth_policy.Policy`. Multiple matching
policies are OR-combined (the union model — a ``DenyAll`` never overrides a
grant; an empty set is fail-closed).

Run with::

    uvicorn full_rbac.app:app --env-file .env

Note the ``--env-file``: ``v2`` does no ``.env`` loading of its own.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI

from full_rbac.message import MessageResource
from full_rbac.models import Message, Thread
from resourcey.v2.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeysConfig
from resourcey.v2.auth.auth_policy import AllowAll, Owner, ReadOnly
from resourcey.v2.auth.auth_rbac import rbac_resource_paths, rbac_resources
from resourcey.v2.auth.auth_rbac_resolver import RbacPolicyResolver
from resourcey.v2.auth.auth_rbac_store import SqlRbacStore
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig
from resourcey.v2.sql.sql_resource import SqlResource

# The stored role names. Stored as plain strings (the ``roles`` table's ``name``
# column), unlike example 04's per-app ``StrEnum`` — a store is server-authoritative,
# so the vocabulary lives in the data, not in the app code.
ADMIN = "admin"
VIEWER = "viewer"
AUTHOR = "author"
DENIED = "denied"

# The REST paths the RBAC resource set is served at, derived from the same
# ``rbac_resources`` builder so a grant can never drift from the served surface.
RBAC_RESOURCE_PATHS: tuple[str, ...] = rbac_resource_paths()

# The permission rules, as (role, resource) -> policy. Seeding writes these into
# ``role_permissions``; the resolver reads them back per request.
#
# Note the resource-scoping: each rule names ONE resource, so a role's grant on
# ``threads`` says nothing about ``messages``.
ROLE_PERMISSIONS: dict[tuple[str, str], Any] = {
    (ADMIN, "threads"): AllowAll(),
    (ADMIN, "messages"): AllowAll(),
    # The admin also administers the RBAC tables themselves. ``rbac_resources``
    # serves each table under its own path, so an admin grant names each one.
    **{(ADMIN, path): AllowAll() for path in RBAC_RESOURCE_PATHS},
    (VIEWER, "threads"): ReadOnly(),
    (VIEWER, "messages"): ReadOnly(),
    (AUTHOR, "threads"): ReadOnly(),
    (AUTHOR, "messages"): Owner(owner_field="author_id"),
}


def build_auth(
    session_source: Any,
    keys: ApiKeysConfig | None = None,
    *,
    cache_ttl_seconds: float | None = None,
) -> tuple[AuthorizedDependencyBuilder, Resource[Any, Any]]:
    """The store-backed RBAC builder plus the exposed key view to register.

    The builder authenticates with an API key (whose ``principal_id`` is the
    stored user id) and resolves policies through
    :class:`~resourcey.v2.auth.auth_rbac_resolver.RbacPolicyResolver`, backed by
    the same database the board uses. ``session_source`` is either an
    ``async_sessionmaker`` or a zero-arg callable returning one (sync or async) —
    the lazy form lets a caller pass a
    :class:`~resourcey.v2.sql.session_manager.SqlSessionManager` that only hands
    out makers once entered.

    ``cache_ttl_seconds`` is the explicit freshness knob: ``None`` (the default)
    resolves the store on **every** request, so a membership change is immediate;
    a positive value bounds staleness (a change is honoured within that window).
    """
    api_keys = keys if keys is not None else ApiKeysConfig.get_instance()
    key_inner: Resource[Any, Any] = config_api_key_resource(api_keys)
    store = SqlRbacStore(session_source)
    resolver_kwargs: dict[str, Any] = {"store": store}
    if cache_ttl_seconds is not None:
        resolver_kwargs["cache_ttl"] = timedelta(seconds=cache_ttl_seconds)
    resolver = RbacPolicyResolver(**resolver_kwargs)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner),
        policy_resolver=resolver,
    )
    return builder, config_api_key_view(key_inner)


def manager_session_source(manager: SqlSessionManager) -> Any:
    """A session source that resolves the manager's maker on each use.

    ``SqlSessionManager`` builds engines lazily and only hands out makers once
    entered (via the manifest lifecycle), so the store cannot hold a maker at
    construction time. Returning the *coroutine* per use lets the store await it,
    and the manager's own caching makes that a cheap lookup after the first.
    """

    async def source() -> Any:
        return await manager.get_session_maker()

    return source


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    keys: ApiKeysConfig | None = None,
    cache_ttl_seconds: float | None = None,
) -> tuple[Manifest, FastAPI]:
    """Build the manifest + FastAPI app.

    A factory so tests can inject their own ``session_manager`` (isolated
    database), ``keys``, or ``cache_ttl_seconds``. The store-backed RBAC builder
    is always wired here, so every app this factory returns is authenticated and
    resolved against the RBAC tables.
    """
    manager = session_manager or SqlSessionManager(SqlConfig.get_instance())
    builder, key_view = build_auth(
        manager_session_source(manager), keys, cache_ttl_seconds=cache_ttl_seconds
    )

    resources: list[Resource[Any, Any]] = [
        SqlResource(Thread, session_manager=manager),
        MessageResource(Message, session_manager=manager),
        key_view,
    ]
    # The full RBAC resource set (model-first over the RBAC tables), served over
    # the ordinary surface. Access is itself governed by the resolver, so an
    # app-wide admin grant is seeded for the admin role.
    resources.extend(rbac_resources(session_manager=manager))

    manifest = Manifest(resources=resources, managers=[manager])
    return manifest, create_app(manifest, dependency_builder=builder)


manifest, app = build_app()
