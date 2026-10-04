"""AsyncAPI documentation for the realtime WebSocket (issue #17).

FastAPI documents the REST surface (OpenAPI) for free; nothing documents the
WebSocket's in-band message protocol (:mod:`~resourcey.realtime.realtime_routes`)
or the per-resource event payloads a subscriber decodes. This module closes
that gap the same way the REST surface is documented -- generatively, from the
manifest -- rather than by hand-maintained prose:
`AsyncAPI <https://www.asyncapi.com/>`_ is to a message-based API what OpenAPI
is to REST, and :func:`generate_asyncapi_document` builds a spec-compliant
(2.6.0) document describing exactly the one WebSocket channel
:func:`~resourcey.realtime.realtime_routes.add_realtime` mounts:

* the six static protocol messages (``subscribe`` / ``unsubscribe`` / client
  ``ping`` published *to* the server; ``ack`` / ``error`` / ``event`` / server
  ``ping`` the server publishes, per
  :mod:`~resourcey.realtime.realtime_routes`'s protocol docstring);
* one ``<resource>Event`` message per **subscribable** resource (the same
  ``Action.READ``-in-exposed-actions gate
  :meth:`~resourcey.realtime.realtime_routes.RealtimeHub.subscribe` enforces,
  so the document never promises a subscription the hub would reject), whose
  ``item`` schema is the resource's own read model
  (``get_rest_models().read_response``) -- the same model the REST ``read`` /
  ``search`` responses already use, and the same security gate a
  :class:`~resourcey.triggers.redis_trigger.RedisTrigger` projects onto before
  publishing (see that module).

:func:`add_asyncapi` mounts the document at a single ``GET`` route (default
``/asyncapi.json``), generated **once at registration time** from the manifest
-- a resource's read model does not change per request, so there is nothing to
regenerate per call.

It also mounts a second ``GET`` route (default ``/asyncapi``) serving an HTML
viewer, exactly the role FastAPI's own ``/docs`` plays for the REST surface:
:func:`get_asyncapi_html` returns a small, static page that loads the
`AsyncAPI React Component <https://github.com/asyncapi/asyncapi-react>`_'s
standalone browser bundle from a CDN and points it at the JSON route -- the
same "CDN script + link to the JSON this app already serves" shape
``fastapi.openapi.docs.get_swagger_ui_html`` uses for Swagger UI, so there is
no new static-asset-serving concern and no bundler step. Both routes are
``include_in_schema=False`` (neither is a REST endpoint).

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from resourcey.core.manifest import Manifest
from resourcey.core.service import Action, normalize_actions
from resourcey.realtime.realtime_event import EventKind
from resourcey.realtime.realtime_routes import DEFAULT_REALTIME_PATH

DEFAULT_ASYNCAPI_PATH = "/asyncapi.json"
DEFAULT_ASYNCAPI_HTML_PATH = "/asyncapi"
DEFAULT_ASYNCAPI_VERSION = "2.6.0"

# A floating major version, exactly how fastapi.openapi.docs pins its own
# Swagger UI CDN default (``swagger-ui-dist@5``) -- a specific minor/patch
# would go stale; the major version is the component's compatibility contract.
DEFAULT_ASYNCAPI_JS_URL = (
    "https://unpkg.com/@asyncapi/react-component@1/browser/standalone/index.js"
)
DEFAULT_ASYNCAPI_CSS_URL = "https://unpkg.com/@asyncapi/react-component@1/styles/default.min.css"

# The static (resource-independent) protocol messages, per the wire protocol
# documented in realtime_routes.py. Kept as plain dicts (not pydantic models)
# since this module's only job is emitting the document, never parsing it --
# realtime_routes.py remains the sole parser.
_SUBSCRIBE_MESSAGE: dict[str, Any] = {
    "name": "subscribe",
    "title": "Subscribe to a resource",
    "payload": {
        "type": "object",
        "required": ["type", "resource"],
        "properties": {
            "type": {"const": "subscribe"},
            "resource": {
                "type": "string",
                "description": "The resource path to subscribe to (e.g. 'threads').",
            },
            "filter": {
                "type": "object",
                "nullable": True,
                "description": (
                    "An optional {'<field>__<op>': value} filter, using the "
                    "same vocabulary as a REST search query string."
                ),
            },
        },
    },
}

_UNSUBSCRIBE_MESSAGE: dict[str, Any] = {
    "name": "unsubscribe",
    "title": "Unsubscribe from a resource",
    "payload": {
        "type": "object",
        "required": ["type", "resource"],
        "properties": {
            "type": {"const": "unsubscribe"},
            "resource": {"type": "string"},
        },
    },
}

_CLIENT_PING_MESSAGE: dict[str, Any] = {
    "name": "clientPing",
    "title": "Client keepalive",
    "payload": {
        "type": "object",
        "required": ["type"],
        "properties": {"type": {"const": "ping"}},
    },
}

_ACK_MESSAGE: dict[str, Any] = {
    "name": "ack",
    "title": "Subscription acknowledged",
    "payload": {
        "type": "object",
        "required": ["type", "resource"],
        "properties": {
            "type": {"const": "ack"},
            "resource": {"type": "string"},
        },
    },
}

_ERROR_MESSAGE: dict[str, Any] = {
    "name": "error",
    "title": "A subscribe / unsubscribe request was rejected",
    "payload": {
        "type": "object",
        "required": ["type", "message"],
        "properties": {
            "type": {"const": "error"},
            "message": {"type": "string"},
        },
    },
}

_SERVER_PING_MESSAGE: dict[str, Any] = {
    "name": "serverPing",
    "title": "Server keepalive",
    "payload": {
        "type": "object",
        "required": ["type"],
        "properties": {"type": {"const": "ping"}},
    },
}


def _event_message(resource_path: str, item_schema_name: str) -> dict[str, Any]:
    """The ``event`` message for one subscribable resource."""
    return {
        "name": f"{resource_path}Event",
        "title": f"A {resource_path} change event",
        "payload": {"$ref": f"#/components/schemas/{item_schema_name}Event"},
    }


def _rename_defs(schema: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Pull a pydantic schema's ``$defs`` up to top-level, prefixed to avoid collisions.

    Two different resources' read models may declare identically named nested
    types (e.g. an ``EventKind``-like enum); prefixing every ``$defs`` key with
    the owning resource's path, and rewriting every ``$ref`` that pointed at
    it, keeps them from colliding once merged into one shared
    ``components.schemas`` map.
    """
    defs = schema.pop("$defs", {})
    renamed = {f"{prefix}__{name}": body for name, body in defs.items()}

    def _rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                return {**node, "$ref": f"#/components/schemas/{prefix}__{name}"}
            return {key: _rewrite(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    schema = _rewrite(schema)
    renamed = {name: _rewrite(body) for name, body in renamed.items()}
    return {prefix: schema, **renamed}


def _item_schema_name(resource_path: str) -> str:
    """The ``components.schemas`` key for ``resource_path``'s read model."""
    return "".join(part.capitalize() for part in resource_path.replace("-", "_").split("_"))


def _event_envelope_schema(item_schema_name: str, item_is_nullable: bool = True) -> dict[str, Any]:
    """The ``ResourceEvent`` envelope schema, parameterized by its ``item`` type."""
    return {
        "type": "object",
        "required": ["resource", "kind", "id", "timestamp"],
        "properties": {
            "resource": {"type": "string"},
            "kind": {"enum": [member.value for member in EventKind]},
            "id": {},
            "timestamp": {"type": "string", "format": "date-time"},
            "item": {
                "oneOf": [{"$ref": f"#/components/schemas/{item_schema_name}"}, {"type": "null"}]
                if item_is_nullable
                else [{"$ref": f"#/components/schemas/{item_schema_name}"}]
            },
        },
    }


def generate_asyncapi_document(
    manifest: Manifest,
    *,
    path: str = DEFAULT_REALTIME_PATH,
    title: str = "Resourcey realtime API",
    version: str = "1.0.0",
    server_url: str = "/",
) -> dict[str, Any]:
    """Build the AsyncAPI 2.6.0 document for ``manifest``'s realtime channel.

    Pure and side-effect free (no FastAPI dependency), so it is directly
    testable without mounting a route.
    """
    messages: dict[str, Any] = {
        "subscribe": _SUBSCRIBE_MESSAGE,
        "unsubscribe": _UNSUBSCRIBE_MESSAGE,
        "clientPing": _CLIENT_PING_MESSAGE,
        "ack": _ACK_MESSAGE,
        "error": _ERROR_MESSAGE,
        "serverPing": _SERVER_PING_MESSAGE,
    }
    schemas: dict[str, Any] = {}
    publish_refs: list[dict[str, str]] = [
        {"$ref": "#/components/messages/subscribe"},
        {"$ref": "#/components/messages/unsubscribe"},
        {"$ref": "#/components/messages/clientPing"},
    ]
    subscribe_refs: list[dict[str, str]] = [
        {"$ref": "#/components/messages/ack"},
        {"$ref": "#/components/messages/error"},
        {"$ref": "#/components/messages/serverPing"},
    ]

    for resource in manifest.resources:
        exposed = resource.get_exposed_resource()
        if exposed is None:
            continue
        supported = normalize_actions(exposed.get_supported_actions())
        if Action.READ not in supported:
            continue
        resource_path = exposed.get_resource_path()
        item_name = _item_schema_name(resource_path)
        item_schema = exposed.get_rest_models().read_response.model_json_schema(
            ref_template="#/$defs/{model}"
        )
        schemas.update(_rename_defs(item_schema, item_name))
        event_schema_name = f"{item_name}Event"
        schemas[event_schema_name] = _event_envelope_schema(item_name)
        message_name = f"{resource_path}Event"
        messages[message_name] = _event_message(resource_path, item_name)
        subscribe_refs.append({"$ref": f"#/components/messages/{message_name}"})

    return {
        "asyncapi": DEFAULT_ASYNCAPI_VERSION,
        "info": {
            "title": title,
            "version": version,
            "description": (
                "The subscription WebSocket's in-band message protocol. "
                "'publish' operations are messages the client sends to the "
                "server; 'subscribe' operations are messages the server "
                "sends to the client."
            ),
        },
        "servers": {
            "default": {"url": server_url, "protocol": "ws"},
        },
        "channels": {
            path: {
                "publish": {"message": {"oneOf": publish_refs}},
                "subscribe": {"message": {"oneOf": subscribe_refs}},
            }
        },
        "components": {
            "messages": messages,
            "schemas": schemas,
        },
    }


def get_asyncapi_html(
    *,
    asyncapi_url: str,
    title: str = "Resourcey realtime API",
    asyncapi_js_url: str = DEFAULT_ASYNCAPI_JS_URL,
    asyncapi_css_url: str = DEFAULT_ASYNCAPI_CSS_URL,
    asyncapi_favicon_url: str = "https://www.asyncapi.com/favicon.ico",
) -> HTMLResponse:
    """Build the HTML page that renders ``asyncapi_url`` with the AsyncAPI viewer.

    Mirrors ``fastapi.openapi.docs.get_swagger_ui_html``: a self-contained page
    loading a CDN JS bundle and CSS file, told where the JSON document already
    served by this app lives. Pure and side-effect free, so it is directly
    testable without mounting a route.
    """
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="{asyncapi_css_url}">
    <link rel="shortcut icon" href="{asyncapi_favicon_url}">
    <title>{title}</title>
    </head>
    <body>
    <div id="asyncapi"></div>
    <script src="{asyncapi_js_url}"></script>
    <script>
        AsyncApiStandalone.render({{
            schema: {{url: "{asyncapi_url}"}},
            config: {{show: {{sidebar: true}}}},
        }}, document.getElementById('asyncapi'));
    </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


def add_asyncapi(
    app_or_router: FastAPI | APIRouter,
    manifest: Manifest,
    *,
    path: str = DEFAULT_REALTIME_PATH,
    asyncapi_path: str = DEFAULT_ASYNCAPI_PATH,
    html_path: str | None = DEFAULT_ASYNCAPI_HTML_PATH,
    title: str = "Resourcey realtime API",
    version: str = "1.0.0",
    server_url: str = "/",
) -> APIRouter:
    """Mount the realtime WebSocket's AsyncAPI document and its HTML viewer.

    Call **after** :func:`~resourcey.http.app.create_app` /
    :func:`~resourcey.realtime.realtime_routes.add_realtime`, mirroring every
    other after-the-fact mount in this framework. The document is generated
    once, here, from the manifest's resources at registration time (a
    resource's read model does not change per request). ``html_path`` (default
    ``/asyncapi``) serves the rendered viewer -- the ``/docs`` equivalent for
    this channel; pass ``html_path=None`` to mount the JSON only.
    """
    document = generate_asyncapi_document(
        manifest, path=path, title=title, version=version, server_url=server_url
    )
    router = APIRouter()

    @router.get(asyncapi_path, include_in_schema=False)
    async def get_asyncapi_document() -> JSONResponse:
        return JSONResponse(document)

    if html_path is not None:

        @router.get(html_path, include_in_schema=False)
        async def get_asyncapi_viewer() -> HTMLResponse:
            return get_asyncapi_html(asyncapi_url=asyncapi_path, title=title)

    app_or_router.include_router(router)
    return router
