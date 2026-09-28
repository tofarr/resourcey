"""Simple-roles example app entry point.

Builds on the API-key-auth example (03) and adds the Part 2 role vocabulary: a
small, per-app set of roles carried on an API key's definition and translated to
policies **in one place** by a
:class:`~resourcey.v2.auth.auth_role.RolePolicyResolver`.

The app has two resources with a deliberate asymmetry:

* ``threads`` — a shared board (no owner): ``ADMIN`` reads/writes everything and
  ``USER`` reads everything it can see (``ReadOnly``).
* ``messages`` — owned by ``author_id``: ``ADMIN`` reads/writes everything,
  ``MODERATOR`` reads/updates everything, and ``USER`` may only read / update /
  delete **its own** rows (the :class:`~resourcey.v2.auth.auth_policy.Owner`
  policy). So a ``USER`` reads *all of* threads but only *its own* messages —
  the example's headline rule.

Roles come from the environment alongside the keys
(``APP_API_KEYS_<n>_ROLES_<m>``), so a presented key's roles are resolved with
**no database lookup**. There is deliberately no role table: roles are Part 2's
simple rung; stored users / groups / roles are Part 3.

Run with::

    uvicorn simple_roles.app:app --env-file .env

Note the ``--env-file``: ``v2`` does no ``.env`` loading of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from resourcey.v2.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeysConfig
from resourcey.v2.auth.auth_policy import AllowAll, Owner, ReadOnly
from resourcey.v2.auth.auth_role import AppRole, RolePolicyResolver
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig
from resourcey.v2.sql.sql_resource import SqlResource
from simple_roles.message import MessageResource
from simple_roles.models import Message, Thread


class Role(AppRole):
    """This app's role vocabulary.

    Roles are **per-app, not global**: the same string means whatever this app
    declares it to mean. Routing names them as ``Role.ADMIN`` (no magic strings)
    while the credential carries the plain value.
    """

    ADMIN = "ADMIN"
    MODERATOR = "MODERATOR"
    USER = "USER"


# The single place this app expresses its role -> policy rules. Global rules
# apply to every resource; per-resource rules are keyed by the resource's path.
# A principal with several roles unions their grants; an un-roled / anonymous
# caller falls to ``default`` (empty => fail-closed).
ROLE_POLICIES = RolePolicyResolver(
    role_policies={
        Role.ADMIN: [AllowAll()],
        Role.MODERATOR: [ReadOnly()],  # further narrowed per resource below
    },
    resource_role_policies={
        "threads": {
            Role.USER: [ReadOnly()],
        },
        "messages": {
            # Read all of threads, but only own rows of messages.
            Role.USER: [Owner(owner_field="author_id")],
            # Moderators read and update every message on top of their
            # read-only global grant.
            Role.MODERATOR: [AllowAll()],
        },
    },
    default=[],
)


# One manager for the whole app; the manifest's lifecycle enters it.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_auth(
    keys: ApiKeysConfig | None = None,
) -> tuple[AuthorizedDependencyBuilder, Resource[Any, Any]]:
    """The role-aware API-key builder plus the exposed key view to register.

    Returns the builder (holding the **inner** key resource to reach
    ``find_by_key``) and the read-hiding view. ``keys`` defaults to
    ``ApiKeysConfig.get_instance()`` (the ``APP_API_KEYS_*`` environment). The
    builder is wired with :data:`ROLE_POLICIES`, so the key's roles are
    translated to policies by the one centralized resolver.
    """
    api_keys = keys if keys is not None else ApiKeysConfig.get_instance()
    key_inner: Resource[Any, Any] = config_api_key_resource(api_keys)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner),
        policy_resolver=ROLE_POLICIES,
    )
    return builder, config_api_key_view(key_inner)


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    keys: ApiKeysConfig | None = None,
) -> tuple[Manifest, FastAPI]:
    """Build the manifest + FastAPI app.

    A factory so tests can inject their own ``session_manager`` (isolated
    database) or ``keys``. The role-aware API-key builder is always wired here,
    so every app this factory returns is authenticated and role-checked.
    """
    manager = session_manager or default_session_manager
    builder, key_view = build_auth(keys)

    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_manager=manager),
            MessageResource(Message, session_manager=manager),
            key_view,
        ],
        managers=[manager],
    )
    return manifest, create_app(manifest, dependency_builder=builder)


manifest, app = build_app()
