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
``/auth/*`` route exists: a request without a valid key is rejected with 401.

Run with::

    uvicorn api_key_auth.app:app

or::

    uvicorn api_key_auth.app:app --reload

Two safeguards stop the example from *appearing* to run while silently serving
unauthenticated traffic. This module loads its own ``.env`` by absolute path, so
starting the server from any directory still picks up the posture; and
:func:`_verify_posture` refuses to expose an app whose builder is not the
API-key one.
"""

from __future__ import annotations

from pathlib import Path

from resourcey.auth2.auth2_api_key import ApiKeyDependencyBuilder
from resourcey.config.config_framework import FrameworkConfig
from resourcey.config.config_loader import load_dotenv
from resourcey.config.config_runtime import get_config_as
from resourcey.manifest import ResourceManifest
from resourcey.resource.errors import ResourceyConfigError

from api_key_auth.message import Message
from api_key_auth.thread import Thread


def _verify_posture() -> None:
    """Fail loudly unless the API-key posture is actually in effect.

    ``FrameworkConfig.dependency_builder`` falls back to a no-auth default when
    ``DEPENDENCY_BUILDER_CLASS`` is unset, so a misconfiguration would otherwise
    serve an open API that merely looks secured. Raise an actionable error at
    import time instead of leaving that to be discovered by a client.
    """
    builder = get_config_as(FrameworkConfig).dependency_builder
    if not isinstance(builder, ApiKeyDependencyBuilder):
        raise ResourceyConfigError(
            "Example 03 requires the API-key posture: set DEPENDENCY_BUILDER_CLASS="
            "resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder "
            f"(resolved {type(builder).__name__} instead)."
        )


# Load this example's .env by absolute path, independent of the process's
# working directory (the default loader looks in the CWD). Real environment
# variables still win, so tests and deployments can override it.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

manifest = ResourceManifest(resources=(Thread, Message))
app = manifest.create_app()
_verify_posture()
