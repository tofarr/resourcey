"""The example's own webhook receiver (issue #18).

A real deployment's ``WebhookTrigger`` points at a separate, independently
owned service -- this example is not that. To stay a **working**, runnable
demo with nothing external to stand up, it mounts a receiving endpoint on the
*same* FastAPI app the triggers notify, so ``uv run uvicorn ...`` is enough to
watch a genuine HTTP delivery end to end: a real ``POST`` leaves
``TriggeredService``, crosses the loopback interface (or, in the test suite,
the very same ASGI app via ``httpx``'s in-process transport), and lands here.

This module owns only the *receiving* side -- logging exactly what arrived, in
a way that is unmistakable in a console full of ``uvicorn`` access-log noise.
Which configured subscriber a delivery came from is read off the URL path
(``/_webhooks/{name}``), not a payload field: the receiver trusts nothing about
the body to identify its sender, only the URL the sender was configured with.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, FastAPI, Request

logger = logging.getLogger("webhooks_example.webhook_receiver")
# Mirrors webhooks_example.triggers' own logger setup: uvicorn's default log
# config leaves the root logger at WARNING, so a plain
# `logging.getLogger(__name__).info(...)` would never print under `uvicorn
# module:app` without this. Giving the logger its own INFO level + handler
# makes the demo watchable with zero setup.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_handler)

WEBHOOK_PATH = "/_webhooks/{name}"

# Headers that merely describe the transport (set by the HTTP client, not the
# webhook sender's own configuration) -- noise in the log line, not signal.
_TRANSPORT_HEADERS = {"host", "content-length", "accept", "accept-encoding", "connection"}


def _format_body(raw: bytes) -> str:
    """Pretty-print a JSON body; fall back to the raw text for anything else."""
    if not raw:
        return "(empty body)"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")
    return json.dumps(parsed, indent=2, sort_keys=True)


def register_webhook_routes(app: FastAPI, *, path: str = WEBHOOK_PATH) -> None:
    """Mount the receiving endpoint.

    Deliberately not one of the eight standard REST actions -- a webhook
    inbox is not a resource -- so it is added directly to ``app``, the same
    "call this after ``create_app``" shape
    :func:`~resourcey.filestore.file_routes.register_file_routes` uses for its
    own non-standard transfer endpoints.
    """
    router = APIRouter(tags=["Webhooks"])

    @router.post(path, summary="Receive a webhook delivery")
    async def receive_webhook(name: str, request: Request) -> dict[str, str]:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _TRANSPORT_HEADERS
        }
        body = _format_body(await request.body())
        banner = "=" * 70
        logger.info(
            "\n%s\nWEBHOOK RECEIVED -- subscriber=%r\n%s\nHeaders: %s\n%s\n%s",
            banner,
            name,
            banner,
            headers,
            body,
            banner,
        )
        return {"status": "received"}

    app.include_router(router)
