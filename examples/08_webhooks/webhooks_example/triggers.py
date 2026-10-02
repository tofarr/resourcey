"""``LoggingWebhookTrigger`` — the example's concrete ``Trigger`` (issue #18).

``resourcey.triggers`` ships only the abstract ``Trigger`` contract and the
generic plumbing (``TriggeredResource`` / ``TriggeredService`` /
``TriggerConfig`` / ``TriggeredDependencyBuilder``); it deliberately ships no
HTTP-sending implementation, so no HTTP client becomes a mandatory framework
dependency (mirroring how ``S3FileStore``'s ``boto3`` stays behind an optional
extra). A concrete webhook sender is the deploying app's job.

This trigger stands in for a real one: instead of making an outbound HTTP
request, it logs — via the stdlib ``logging`` module — exactly what it *would*
have sent and to which configured URL. That is enough to prove the mechanism
end to end (fires once per write, only on success, per-trigger isolated, in
the background by default) without a second server/process or any network
access, which is what makes this example runnable with nothing but
``uv run uvicorn``.

A real implementation would replace the ``logger.info(...)`` call below with,
e.g.::

    async with httpx.AsyncClient() as client:
        await client.post(self.url, json=[...])
"""

from __future__ import annotations

import logging
from typing import Any

from resourcey.core.service import Create, Delete, Update
from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults

logger = logging.getLogger("webhooks_example.webhook")
# uvicorn's default log config only raises the level of its own "uvicorn*"
# loggers; the root logger is left at WARNING, so a plain
# `logging.getLogger(__name__).info(...)` would silently never print under
# `uvicorn module:app`. Setting this logger's own level to INFO and giving it
# its own handler makes the demo watchable with zero setup, regardless of how
# (or whether) the host app configures logging. Propagation is left on (the
# default) -- deliberately: it costs nothing when nothing is listening higher
# up, and it is what lets a test capture these records with plain `caplog`.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_handler)


def _describe(edit: Create[Any] | Update[Any] | Delete[Any], result: Any) -> str:
    """A short, human-readable description of one edit/result pair for the log line."""
    if isinstance(edit, Delete):
        return f"delete id={edit.id!r}"
    kind = "create" if isinstance(edit, Create) else "update"
    if result is None:
        return f"{kind} -> miss"
    dump = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return f"{kind} -> {dump}"


class LoggingWebhookTrigger(Trigger[Any, Any]):
    """Logs what it would deliver to ``url``, instead of actually delivering it.

    Attributes:
        name: A label for the simulated subscriber (e.g. ``"audit-log"``,
            ``"slack-notify"``) — printed in every log line so multiple
            configured triggers are distinguishable in the console.
        url: The webhook URL a real implementation would ``POST`` to. Never
            actually called by this trigger; carried only so the log line
            reads the way a real delivery's would.
    """

    name: str
    url: str = "https://example.com/webhooks/inbox"

    async def callback(self, edits: TriggerEdits[Any, Any], results: TriggerResults[Any]) -> None:
        """Log one line for the whole operation -- never one line per item.

        ``edits`` / ``results`` already carry the *entire* batch for a single
        ``batch_edit`` call (see the ``Trigger`` docstring): a single log line
        here is what makes "fires once per operation" visibly true in the
        console, rather than merely true of the Python call count.
        """
        pairs = [_describe(edit, result) for edit, result in zip(edits, results, strict=True)]
        summary = pairs[0] if len(pairs) == 1 else "; ".join(pairs)
        logger.info("[webhook:%s] would POST to %s: %s", self.name, self.url, summary)
