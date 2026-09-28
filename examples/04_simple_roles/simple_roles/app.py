"""Simple-roles example app entry point.

Builds on the API-key-auth example (03) and adds the Part 2 role vocabulary: a
small, per-app set of roles carried on an API key's definition and translated to
policies **in one place** by a
:class:`~resourcey.auth.auth_role.RolePolicyResolver`.

Every resource is **readable by default** — anonymous callers included — because
reads are public reference data on this board. What a role *gates* is the
writes:

* ``threads`` — a shared board (no owner): everyone reads it; only ``ADMIN``
  writes it.
* ``messages`` — owned by ``author_id``: everyone reads it, a ``USER`` may
  create, and may update / delete **its own** rows (the
  :class:`~resourcey.auth.auth_policy.Owner` policy), and a ``MODERATOR``
  may create / update / delete any row. So a ``USER`` reads *all of* threads and
  messages but only edits *its own* messages — the example's headline rule.
* ``users`` — the stored **principal**. A key's ``PRINCIPAL_ID`` must name an
  ``enabled`` ``User`` row (the authenticator validates it), and the resource is
  admin-only: no role maps a ``User`` grant, so only ``ADMIN`` may read it. Its
  write routes are never mounted (the view narrows it to the read subset).

The default posture is ``OPTIONAL``: no credential is anonymous, which is what
lets reads be public. A *presented but invalid* key is still rejected, so the
writes cannot be reached with a bad credential. Reads are gated by the
(resource-level) default ``ReadOnly`` every role inherits; writes need a role
grant.

Roles come from the environment alongside the keys
(``APP_API_KEYS_<n>_ROLES_<m>``), so a presented key's roles are resolved with
**no database lookup**. The principal a key acts as, however, *is* a stored row
(``APP_API_KEYS_<n>_PRINCIPAL_ID`` names it), so a principal can be disabled and
the change takes effect on the next request.

Run with::

    uvicorn simple_roles.app:app --env-file .env

Note the ``--env-file``: the framework does no ``.env`` loading of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from resourcey.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_config import ApiKeysConfig
from resourcey.auth.auth_policy import AllowAll, Owner, ReadOnly
from resourcey.auth.auth_role import AppRole, RolePolicyResolver
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.http.app import create_app
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.sql.sql_resource import SqlResource
from simple_roles.message import MessageResource
from simple_roles.models import Message, Thread
from simple_roles.user import user_resource, user_view


class Role(AppRole):
    """This app's role vocabulary.

    Roles are **per-app, not global**: the same string means whatever this app
    declares it to mean. Routing names them as ``Role.ADMIN`` (no magic strings)
    while the credential carries the plain value.
    """

    ADMIN = "ADMIN"
    MODERATOR = "MODERATOR"
    USER = "USER"


# The single place this app expresses its role -> policy rules. Reads are public:
# every role inherits the resource-level default ``ReadOnly``, and so does an
# anonymous caller (the default maps to every resource and to ``default``). A
# role only *adds* write grants per resource; the union model means the inherited
# ``ReadOnly`` never licenses a write on its own.
#
# ``threads`` has no write grant beyond ``ADMIN``'s global ``AllowAll``; a USER /
# MODERATOR therefore reads it but cannot write.
ROLE_POLICIES = RolePolicyResolver(
    role_policies={
        Role.ADMIN: [AllowAll()],
    },
    resource_role_policies={
        "messages": {
            # A USER reads every message but writes only its own. ``ReadOnly``
            # grants the read-like actions over every row and ``Owner`` scopes the
            # by-id writes to ``author_id``; the union model OR-combines them, and
            # ``OR(All, own) == All`` for reads, so the two roles compose to
            # read-all / write-own rather than narrowing the read.
            Role.USER: [ReadOnly(), Owner(owner_field="author_id")],
            # A MODERATOR creates / updates / deletes any message.
            Role.MODERATOR: [AllowAll()],
        },
    },
    # Reads are public: the resource-level default grants the read-like actions to
    # every caller that matched no role (an anonymous caller, or an un-roled key),
    # while writes still require a role grant.
    # ``users`` is deliberately absent: the stored principals are not public, so
    # no role maps a ``User`` grant and only ``ADMIN``'s global ``AllowAll``
    # reaches it (everything else falls to the fail-closed ``default``).
    resource_defaults={
        "threads": [ReadOnly()],
        "messages": [ReadOnly()],
    },
    default=[],
)


# One manager for the whole app; the manifest's lifecycle enters it.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_auth(
    keys: ApiKeysConfig | None = None,
    *,
    users: Resource[Any, Any] | None = None,
) -> tuple[AuthorizedDependencyBuilder, Resource[Any, Any]]:
    """The role-aware API-key builder plus the exposed key view to register.

    Returns the builder (holding the **inner** key resource to reach
    ``find_by_key``, and the **inner** user resource to validate a key's
    principal) and the read-hiding key view. ``keys`` defaults to
    ``ApiKeysConfig.get_instance()`` (the ``APP_API_KEYS_*`` environment). The
    builder is wired with :data:`ROLE_POLICIES`, so the key's roles are
    translated to policies by the one centralized resolver.
    """
    api_keys = keys if keys is not None else ApiKeysConfig.get_instance()
    key_inner: Resource[Any, Any] = config_api_key_resource(api_keys)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner, user_resource=users),
        policy_resolver=ROLE_POLICIES,
        # Reads are public, so an absent credential is anonymous rather than a
        # 401; a *presented but invalid* key is still rejected.
        posture=Posture.OPTIONAL,
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
    so every app this factory returns is authenticated, role-checked, and
    resolves each key's principal against the stored ``User`` rows.
    """
    manager = session_manager or default_session_manager
    users_inner = user_resource(session_manager=manager)
    builder, key_view = build_auth(keys, users=users_inner)

    manifest = Manifest(
        resources=[
            SqlResource(Thread, session_manager=manager),
            MessageResource(Message, session_manager=manager),
            key_view,
            user_view(users_inner),
        ],
        managers=[manager],
    )
    return manifest, create_app(manifest, dependency_builder=builder)


manifest, app = build_app()
