"""Users-and-permissions example app entry point.

Builds on example 01's message board (``Thread`` + ``Message``) by adding
``User`` and ``UserPermission`` resources and serving all four through
:class:`~resourcey.auth.secured_service.SecuredService` wrappers. The dev IdP
(``/auth/dev``) issues session cookies the secured services resolve into a
principal, and every resource action is permission-checked against the
principal's ``UserPermission`` rows (fail-closed — no access without an
explicit grant).

Run with::

    uvicorn users_and_permissions.app:app

or::

    uvicorn users_and_permissions.app:app --reload
"""

from __future__ import annotations

from resourcey.auth.auth_router import router as auth_router
from resourcey.auth.dev_router import router as dev_router
from resourcey.manifest import ResourceManifest

from users_and_permissions.message import Message
from users_and_permissions.thread import Thread
from users_and_permissions.user import User
from users_and_permissions.user_permission import UserPermission

manifest = ResourceManifest(
    resources=(Thread, Message, User, UserPermission),
)
app = manifest.create_app()

# Wire the federated OAuth + dev IdP routers onto the manifest-built app so
# the login flow (``POST /auth/dev/login``) mints the session cookie the
# secured services resolve into a principal.
app.include_router(auth_router)
app.include_router(dev_router)
