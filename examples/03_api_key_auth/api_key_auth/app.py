"""API-key-auth example app entry point.

Builds on example 01's message board (``Thread`` + ``Message``) and secures the
whole REST API with environment-configured API keys — no users, no sessions, no
auth tables, and no ``/auth/*`` routes.

The ``v2`` app is assembled from the same three pieces as examples 01 / 02: a
:class:`~resourcey.v2.sql.session_manager.SqlSessionManager` (the engines, built
from ``APP_SQL_CONNECTIONS_*``), a
:class:`~resourcey.v2.core.manifest.Manifest` (the resource set and its
lifecycle), and the :func:`~resourcey.v2.http.app.create_app` free function.
The security posture is the fourth piece: ``create_app``'s
``dependency_builder=`` argument, handed an
:class:`~resourcey.v2.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
that authenticates with an
:class:`~resourcey.v2.auth.auth_api_key.ApiKeyAuthenticator` and then grants the
authenticated principal full access (the default ``AllowAllResolver``). The key
check is composed in front of every resource's service dependency.

Run with::

    uvicorn api_key_auth.app:app --env-file .env

Note the ``--env-file``: ``v2`` does no ``.env`` loading of its own, so the
process environment must be populated by the caller (uvicorn, or a shell).

Key source. The accepted keys come from the environment
(``APP_API_KEYS_0_ID`` / ``_KEY``, ``APP_API_KEYS_1_*``, …, or the JSON-array
form) via :class:`~resourcey.v2.auth.auth_config.ApiKeysConfig`, exposed as a
read-only :class:`~resourcey.v2.list.list_resource.ListResource` built from that
config. The keys are hashed on load and never stored in a table, so the schema
is just ``threads`` and ``messages`` and there is nothing to migrate for auth.

The authenticator holds the **inner** key resource (it needs ``find_by_key``)
while the manifest registers the **view** over it (which hides the key digest
from every response and from the query surface).

:func:`build_app` is the only assembly path and always wires the API-key builder
via ``create_app(..., dependency_builder=builder)``, so every app it returns is
authenticated. The framework's no-auth default is named
:class:`~resourcey.v2.http.dependency_builder.OpenDependencyBuilder` precisely so
that dropping that argument reads as an explicit choice to serve an open API
rather than a quiet fallback.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from api_key_auth.message import MessageResource
from api_key_auth.models import Message, Thread
from resourcey.v2.auth.auth_api_key import ApiKeyAuthenticator
from resourcey.v2.auth.auth_api_key_resource import config_api_key_resource, config_api_key_view
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeysConfig
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.core.resource import Resource
from resourcey.v2.http.app import create_app
from resourcey.v2.sql.session_manager import SqlSessionManager
from resourcey.v2.sql.sql_config import SqlConfig
from resourcey.v2.sql.sql_resource import SqlResource

# One manager for the whole app; it is entered by the manifest's lifecycle.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_auth(
    keys: ApiKeysConfig | None = None,
) -> tuple[AuthorizedDependencyBuilder, Resource[Any, Any]]:
    """The API-key authenticator plus the exposed view over its key resource.

    Returns the builder (which holds the **inner** key resource and reaches
    ``find_by_key`` on it) and the read-hiding view to register in the manifest.
    ``keys`` defaults to ``ApiKeysConfig.get_instance()`` (the ``APP_API_KEYS_*``
    environment).
    """
    api_keys = keys if keys is not None else ApiKeysConfig.get_instance()
    key_inner: Resource[Any, Any] = config_api_key_resource(api_keys)
    builder = AuthorizedDependencyBuilder(
        authenticator=ApiKeyAuthenticator(key_resource=key_inner)
    )
    return builder, config_api_key_view(key_inner)


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    keys: ApiKeysConfig | None = None,
) -> tuple[Manifest, FastAPI]:
    """Build the manifest + FastAPI app.

    Kept as a factory (rather than wiring only at import time) so the tests can
    build a fresh app and inject their own ``session_manager`` (e.g. one pointing
    at an isolated database) or ``keys`` without touching the resources.
    ``session_manager`` defaults to :data:`default_session_manager`.

    The API-key builder is always wired here, so every app this factory returns
    is authenticated.
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
