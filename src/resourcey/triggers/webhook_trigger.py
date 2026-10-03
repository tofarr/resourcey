"""``WebhookTrigger`` — the framework's own HTTP-delivering ``Trigger`` (issue #155).

:mod:`~resourcey.triggers.trigger` ships only the abstract ``Trigger``
contract; this module is the generic, concrete webhook sender built on top of
it — the framework's answer to "notify an external system over HTTP when rows
change". It is deliberately kept behind the ``httpx`` import: building a real
client without the ``webhooks`` extra (``resourcey[webhooks]``) raises an
actionable :class:`ImportError`, the same lazy-import shape
:class:`~resourcey.filestore.s3_file_store.S3FileStore` uses for ``boto3`` —
so no HTTP client becomes a mandatory framework dependency merely because an
app wants to attach one trigger.

A ``WebhookTrigger`` carries exactly three delivery-shaping fields:

* ``url`` — **required, no default**. A webhook with no destination would
  silently deliver nowhere, so the one field that determines where every
  request goes must be supplied explicitly.
* ``headers`` — a list of :class:`WebhookHeader` (``name`` + a
  :class:`~pydantic.SecretStr` ``value``), sent with every request. A typical
  use is a signing secret or bearer token the receiving endpoint checks;
  ``SecretStr`` keeps it redacted from default serialization / ``repr`` /
  logging, the same convention :class:`~resourcey.sql.db_config.DbConfig`'s
  ``password`` and :class:`~resourcey.auth.auth_config.ApiKeyConfig`'s ``key``
  use.
* ``retry`` — a polymorphic :class:`RetryStrategy` (default
  :class:`NoRetry`, i.e. a single attempt). A deployment opts into retries
  explicitly rather than the framework silently retrying (or not) on its
  behalf.

``callback`` fires once per edit operation (never per item — see the
``Trigger`` docstring), ``POST``-ing a JSON array of ``{"kind", "item"/"id",
"result"}`` records, one per ``(edit, result)`` pair, aligned exactly with
``edits`` / ``results``. A non-2xx response or a transport error is retried
per ``retry.delays()``; exhausting every attempt re-raises the last error,
which :class:`~resourcey.triggers.trigger_runner.TriggerRunner` isolates and
logs — this trigger raising never stops another configured trigger, and never
fails the request that triggered it (see ``triggered_service.py``).

An explicit ``client=`` constructor argument (mirroring ``S3FileStore``) lets
a caller inject its own HTTP client — a real one pointed at a non-default
transport, or a test double — so delivery is testable without a real socket.
:meth:`WebhookTrigger.bind_client` is the escape hatch for the rarer case
where that client can only be built *after* the trigger already exists (e.g.
a client bound to the very app the trigger will notify, which cannot exist
before the app does — see ``examples/08_webhooks``).

This module is part of ``resourcey.triggers``; it imports only lower
framework layers (``core`` / ``util``), plus its sibling :mod:`trigger`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, SecretStr, model_validator

from resourcey.core.service import Create, Delete, Update
from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults
from resourcey.util.models import DiscriminatedUnionMixin


class WebhookHeader(BaseModel):
    """One HTTP header a :class:`WebhookTrigger` sends with every request.

    Attributes:
        name: The header name (e.g. ``"X-Webhook-Secret"``, ``"Authorization"``).
        value: The header's plaintext, read as a :class:`~pydantic.SecretStr`
            so it is redacted by default serialization / ``repr`` / logging —
            only this trigger's own delivery code reads the plaintext (via
            :meth:`~pydantic.SecretStr.get_secret_value`).
    """

    name: str
    value: SecretStr


class RetryStrategy(DiscriminatedUnionMixin, ABC):
    """Abstract, polymorphic retry policy for a :class:`WebhookTrigger` delivery.

    :meth:`delays` is the whole contract: the number of seconds to sleep
    before each *retry* attempt (never the first). An empty list means the
    first attempt is the only attempt; a list of length ``n`` means up to
    ``n`` retries after an initial failure, so up to ``n + 1`` attempts total.
    """

    @abstractmethod
    def delays(self) -> list[float]:
        """The delay, in seconds, before each retry attempt (empty = no retries)."""
        raise NotImplementedError


class NoRetry(RetryStrategy):
    """No retries: a single delivery attempt. The default for a new trigger."""

    def delays(self) -> list[float]:
        return []


class FixedDelayRetry(RetryStrategy):
    """``max_retries`` retries, each after the same fixed ``delay_seconds``."""

    max_retries: int = 3
    delay_seconds: float = 1.0

    @model_validator(mode="after")
    def _validate(self) -> FixedDelayRetry:
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.delay_seconds < 0:
            raise ValueError(f"delay_seconds must be >= 0, got {self.delay_seconds}")
        return self

    def delays(self) -> list[float]:
        return [self.delay_seconds] * self.max_retries


class ExponentialBackoffRetry(RetryStrategy):
    """``max_retries`` retries, each delay multiplied by ``multiplier``, capped at ``max_delay_seconds``."""

    max_retries: int = 3
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 60.0

    @model_validator(mode="after")
    def _validate(self) -> ExponentialBackoffRetry:
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.initial_delay_seconds < 0:
            raise ValueError(
                f"initial_delay_seconds must be >= 0, got {self.initial_delay_seconds}"
            )
        if self.multiplier < 1:
            raise ValueError(f"multiplier must be >= 1, got {self.multiplier}")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be >= initial_delay_seconds")
        return self

    def delays(self) -> list[float]:
        result: list[float] = []
        delay = self.initial_delay_seconds
        for _ in range(self.max_retries):
            result.append(min(delay, self.max_delay_seconds))
            delay *= self.multiplier
        return result


def _import_httpx() -> Any:
    """Import ``httpx``, or raise an actionable ``ImportError`` naming the extra."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "WebhookTrigger requires httpx; install it with 'resourcey[webhooks]'."
        ) from exc
    return httpx


def _json_safe(value: Any) -> Any:
    """``model_dump(mode="json")`` a DTO instance; pass ``None`` / plain values through."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _edit_payload(edit: Create[Any] | Update[Any] | Delete[Any], result: Any) -> dict[str, Any]:
    """One wire record for a single ``(edit, result)`` pair.

    ``create`` / ``update`` carry the client's ``item`` alongside the
    service's ``result`` (``None`` on a miss); ``delete`` carries only the
    target ``id`` — there is no item and the result is always ``None``.
    """
    if isinstance(edit, Delete):
        return {"kind": "delete", "id": _json_safe(edit.id)}
    kind = "create" if isinstance(edit, Create) else "update"
    return {"kind": kind, "item": _json_safe(edit.item), "result": _json_safe(result)}


class WebhookTrigger(Trigger[Any, Any]):
    """Delivers edit-event notifications to ``url`` over HTTP, with configurable retry.

    See the module docstring for the field contract and the wire payload
    shape. ``timeout_seconds`` bounds a single delivery attempt (not the
    whole retry sequence).
    """

    url: str
    headers: list[WebhookHeader] = Field(default_factory=list)
    retry: RetryStrategy = Field(default_factory=NoRetry)
    timeout_seconds: float = 10.0

    _client: Any = PrivateAttr(default=None)

    def __init__(self, *, client: Any = None, **data: Any) -> None:
        super().__init__(**data)
        self._client = client

    def bind_client(self, client: Any) -> None:
        """Attach an already-built HTTP client for this trigger to deliver through.

        The constructor's ``client=`` argument is the usual injection point
        (a trigger built with a known destination up front); this is the
        escape hatch for the rarer case where the client can only be built
        *after* the trigger already exists — see the module docstring.
        """
        self._client = client

    async def callback(self, edits: TriggerEdits[Any, Any], results: TriggerResults[Any]) -> None:
        body = [_edit_payload(edit, result) for edit, result in zip(edits, results, strict=True)]
        header_map = {header.name: header.value.get_secret_value() for header in self.headers}
        if self._client is not None:
            await self._deliver(self._client, body, header_map)
            return
        httpx = _import_httpx()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            await self._deliver(client, body, header_map)

    async def _deliver(
        self, client: Any, body: list[dict[str, Any]], headers: dict[str, str]
    ) -> None:
        """Attempt delivery, retrying per ``self.retry.delays()`` on any failure."""
        delays = self.retry.delays()
        for attempt in range(len(delays) + 1):
            if attempt > 0:
                await asyncio.sleep(delays[attempt - 1])
            try:
                response = await client.post(self.url, json=body, headers=headers)
                response.raise_for_status()
                return
            except Exception:
                if attempt == len(delays):
                    raise
