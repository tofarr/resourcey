"""OAuth example app entry point (issue #151, Part 4 of the auth roadmap).

Builds on the message board of examples 01 / 03 / 04 and federates
authentication to an **external identity provider**: a caller presents a token
the provider issued, the framework verifies it, maps it to a local user, and the
ordinary authorization rules decide what it may do.

The pieces, all from :mod:`resourcey.auth`:

* the **client config** — :class:`~resourcey.auth.auth_oauth_config.IdpConfig`,
  parsed from ``APP_OAUTH_CLIENTS_<n>_*`` and served read-only (the
  ``client_secret`` is projected away);
* the **identity map** — an ``external_identities`` row linking a provider
  ``(issuer, subject)`` to our internal ``user_id``;
* the **token store** — an ``oauth_tokens`` row, encrypted at rest, holding the
  refresh token the interactive flow persists;
* the **local user store** — the ``users`` table, read-only, which the resolved
  identity is validated against (a disabled / missing local user is rejected,
  however valid the provider's token — the local store stays authoritative);
* :class:`~resourcey.auth.auth_oauth.OAuthAuthenticator` — the inbound verifier
  (issuer-keyed lookup, JWKS signature check, ``alg`` pinned to the row's
  allowlist, ``(iss, sub)`` mapped to the internal user);
* :func:`~resourcey.auth.auth_oauth_routes.register_oauth_routes` — the
  interactive login / callback / refresh flow, mounted **after** ``create_app``
  (the ``06_filestore`` pattern), which mints **our own** BFF session cookie.

Authorization is the app's role rules: reads are public, a ``USER`` may create a
message and edit only its own. The roles come off the **client row**
(``APP_OAUTH_CLIENTS_<n>_ROLES_<m>``) and/or the token's roles claim — either way
the ``RolePolicyResolver`` translates them in one place.

Run with::

    uvicorn oauth_example.app:app --env-file .env --port 8087

Note the ``--env-file``: the framework does no ``.env`` loading of its own.

For a runnable local demo the app serves its own **dev JWKS** and injects a
matching JWKS fetcher, so a token signed with the dev key verifies without an
external IdP. A real deployment points ``APP_OAUTH_CLIENTS_0_JWKS_URI`` at the
provider and drops the injection — the verification code path is identical.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from oauth_example.dev_idp import dev_jwks_get
from oauth_example.message import MessageResource
from oauth_example.models import Message, Thread
from oauth_example.user import user_resource, user_view
from resourcey.auth.auth_authorized_dependency import AuthorizedDependencyBuilder, Posture
from resourcey.auth.auth_cookie import CookieAuthenticator
from resourcey.auth.auth_oauth_config import IdpConfig
from resourcey.auth.auth_oauth_setup import RUNG_CONFIG, OAuthSetup, configure_oauth
from resourcey.auth.auth_policy import AllowAll, Owner, ReadOnly
from resourcey.auth.auth_principal import CompositeAuthenticator
from resourcey.auth.auth_role import AppRole, RolePolicyResolver
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.http.app import create_app
from resourcey.sql.session_manager import SqlSessionManager
from resourcey.sql.sql_config import SqlConfig
from resourcey.sql.sql_resource import SqlResource


class Role(AppRole):
    """This app's role vocabulary (per-app, not global)."""

    ADMIN = "ADMIN"
    USER = "USER"


# The one place this app expresses its role -> policy rules. Reads are public:
# every resource falls to the resource-level ``ReadOnly`` default, so an
# anonymous caller reads the board. A role only *adds* write grants; the union
# model means the inherited ``ReadOnly`` never licenses a write on its own.
ROLE_POLICIES = RolePolicyResolver(
    role_policies={
        Role.ADMIN: [AllowAll()],
    },
    resource_role_policies={
        "messages": {
            # A USER reads every message but writes only its own.
            Role.USER: [ReadOnly(), Owner(owner_field="author_id")],
        },
    },
    resource_defaults={
        "threads": [ReadOnly()],
        "messages": [ReadOnly()],
    },
    default=[],
)


# One manager for the whole app; the manifest's lifecycle enters it.
default_session_manager = SqlSessionManager(SqlConfig.get_instance())


def build_app(
    *,
    session_manager: SqlSessionManager | None = None,
    session_factory: Any = None,
    config: IdpConfig | None = None,
    http_get: Any = None,
    http_post: Any = None,
) -> tuple[Manifest, FastAPI, OAuthSetup]:
    """Build the manifest + FastAPI app, wiring the whole OAuth subsystem.

    A factory so tests can inject their own session source (isolated database)
    and a fake JWKS fetcher / token poster (so no network is touched).
    ``configure_oauth`` builds the client resource, the token + identity
    resources, and a ready ``OAuthAuthenticator``; the same pieces feed the flow
    routes, so an entry point reads like the other examples rather than wiring
    five objects by hand.

    ``session_factory`` is the escape hatch for a caller that already holds a
    maker (the tests); otherwise ``session_manager`` (default
    :data:`default_session_manager`) supplies the engines.
    """
    if session_factory is not None:
        session_kwargs: dict[str, Any] = {"session_factory": session_factory}
        managers: list[Any] = []
    else:
        manager = session_manager or default_session_manager
        session_kwargs = {"session_manager": manager}
        managers = [manager]

    users_inner = user_resource(**session_kwargs)

    # Production points the client's ``jwks_uri`` at the provider and leaves
    # ``http_get`` unset (the default httpx fetch). The example injects the dev
    # JWKS fetcher so a dev token verifies without an external IdP.
    resolved_http_get = http_get if http_get is not None else dev_jwks_get
    setup = configure_oauth(
        config,
        rung=RUNG_CONFIG,
        user_resource=users_inner,
        http_get=resolved_http_get,
        **session_kwargs,
    )
    builder = AuthorizedDependencyBuilder(
        # The BFF posture: the browser presents *our* session cookie (minted by
        # the callback), while a machine client may present a provider bearer
        # token directly — so the two authenticators compose, first-match-wins.
        authenticator=CompositeAuthenticator(
            authenticators=[
                CookieAuthenticator(
                    cookie_name=setup.session_config.session_cookie_name,
                    encryption_service=setup.encryption_service,
                ),
                setup.authenticator,
            ]
        ),
        policy_resolver=ROLE_POLICIES,
        # Reads are public, so an absent credential is anonymous rather than a
        # 401; a *presented but invalid* credential is still rejected.
        posture=Posture.OPTIONAL,
    )

    resources: list[Resource[Any, Any]] = [
        SqlResource(Thread, **session_kwargs),
        MessageResource(Message, **session_kwargs),
        setup.client_view,
        setup.identity_resource,
        user_view(users_inner),
    ]
    manifest = Manifest(resources=resources, managers=managers)
    app = create_app(manifest, dependency_builder=builder)
    # The interactive flow is mounted after create_app, reading the same client
    # resource. ``http_post`` is injected in tests; production wires the default
    # httpx poster.
    setup.register_routes(app, http_post=http_post)
    return manifest, app, setup


manifest, app, setup = build_app()
