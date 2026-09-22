"""API-key-auth example app entry point.

Builds on example 01's message board (``Thread`` + ``Message``) and secures the
whole REST API with a single environment-configured API key, with no users, no
sessions, and no auth tables.

The security posture is one config value. ``.env`` sets::

    DEPENDENCY_BUILDER_CLASS=resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder
    DEPENDENCY_BUILDER_API_KEYS_0=example-api-key

so ``FrameworkConfig.dependency_builder`` resolves to the auth2
:class:`~resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder`, and every
resource's service dependency requires a valid key before it opens storage
(issue #62's seam). No resource file mentions authentication, and no
``/auth/*`` route exists: a request without a valid key is rejected with 403.

Run with::

    uvicorn api_key_auth.app:app

or::

    uvicorn api_key_auth.app:app --reload
"""

from __future__ import annotations

from resourcey.manifest import ResourceManifest

from api_key_auth.message import Message
from api_key_auth.thread import Thread

manifest = ResourceManifest(resources=(Thread, Message))
app = manifest.create_app()
