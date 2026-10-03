"""Tests for ``resourcey.triggers.webhook_trigger`` (the generic ``WebhookTrigger``).

Exercised against real code paths: a real ``WebhookTrigger.callback()`` call,
a real :class:`~resourcey.triggers.webhook_trigger.RetryStrategy` computing
real delays, and an injected fake HTTP client (a minimal duck-typed double —
not a mock of the trigger itself) so delivery is observable without a real
socket. ``asyncio.sleep`` between retries is real too (the test strategies use
very small delays rather than monkeypatching time).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from resourcey.core.service import Create, Delete, Update
from resourcey.triggers.webhook_trigger import (
    ExponentialBackoffRetry,
    FixedDelayRetry,
    NoRetry,
    RetryStrategy,
    WebhookHeader,
    WebhookTrigger,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _RecordingClient:
    """A minimal duck-typed stand-in for ``httpx.AsyncClient``.

    ``fail_times`` lets a test make the first N calls fail (a 500 response)
    before succeeding, to exercise retry without real network flakiness.
    """

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[tuple[str, Any, dict[str, str]]] = []

    async def post(self, url: str, json: Any, headers: dict[str, str]) -> _FakeResponse:
        self.calls.append((url, json, headers))
        if len(self.calls) <= self.fail_times:
            return _FakeResponse(500)
        return _FakeResponse(200)


class _AlwaysFailsClient:
    def __init__(self) -> None:
        self.calls = 0

    async def post(self, url: str, json: Any, headers: dict[str, str]) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(500)


# ---------------------------------------------------------------------------
# Field contract
# ---------------------------------------------------------------------------


class TestFields:
    def test_url_is_required(self) -> None:
        with pytest.raises(ValidationError, match="url"):
            WebhookTrigger()

    def test_defaults(self) -> None:
        trigger = WebhookTrigger(url="http://example.com/hook")
        assert trigger.headers == []
        assert isinstance(trigger.retry, NoRetry)
        assert trigger.timeout_seconds == 10.0

    def test_header_value_is_redacted_by_default(self) -> None:
        trigger = WebhookTrigger(
            url="http://example.com/hook",
            headers=[WebhookHeader(name="X-Secret", value=SecretStr("super-secret"))],
        )
        dumped = trigger.model_dump()
        assert dumped["headers"][0]["value"].get_secret_value() == "super-secret"
        assert "super-secret" not in repr(trigger)
        assert "super-secret" not in str(trigger)


# ---------------------------------------------------------------------------
# RetryStrategy built-ins
# ---------------------------------------------------------------------------


class TestRetryStrategies:
    def test_no_retry_has_no_delays(self) -> None:
        assert NoRetry().delays() == []

    def test_fixed_delay_repeats_the_same_delay(self) -> None:
        assert FixedDelayRetry(max_retries=3, delay_seconds=2.5).delays() == [2.5, 2.5, 2.5]

    def test_fixed_delay_rejects_negative_retries(self) -> None:
        with pytest.raises(ValidationError, match="max_retries"):
            FixedDelayRetry(max_retries=-1)

    def test_fixed_delay_rejects_negative_delay(self) -> None:
        with pytest.raises(ValidationError, match="delay_seconds"):
            FixedDelayRetry(delay_seconds=-1)

    def test_exponential_backoff_doubles_and_caps(self) -> None:
        strategy = ExponentialBackoffRetry(
            max_retries=4, initial_delay_seconds=1, multiplier=2, max_delay_seconds=5
        )
        assert strategy.delays() == [1.0, 2.0, 4.0, 5.0]

    def test_exponential_backoff_rejects_negative_retries(self) -> None:
        with pytest.raises(ValidationError, match="max_retries"):
            ExponentialBackoffRetry(max_retries=-1)

    def test_exponential_backoff_rejects_negative_initial_delay(self) -> None:
        with pytest.raises(ValidationError, match="initial_delay_seconds"):
            ExponentialBackoffRetry(initial_delay_seconds=-1)

    def test_exponential_backoff_rejects_multiplier_below_one(self) -> None:
        with pytest.raises(ValidationError, match="multiplier"):
            ExponentialBackoffRetry(multiplier=0.5)

    def test_exponential_backoff_rejects_max_below_initial(self) -> None:
        with pytest.raises(ValidationError, match="max_delay_seconds"):
            ExponentialBackoffRetry(initial_delay_seconds=10, max_delay_seconds=1)

    def test_abstract_base_rejects_direct_instantiation(self) -> None:
        with pytest.raises(TypeError):
            RetryStrategy()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


class TestPayloadShape:
    async def test_create_and_update_carry_item_and_result(self) -> None:
        client = _RecordingClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        await trigger.callback(
            [Create(item={"title": "a"}), Update(item={"title": "b"})],
            [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}],
        )
        _, body, _ = client.calls[0]
        assert body == [
            {"kind": "create", "item": {"title": "a"}, "result": {"id": 1, "title": "a"}},
            {"kind": "update", "item": {"title": "b"}, "result": {"id": 2, "title": "b"}},
        ]

    async def test_delete_carries_only_id(self) -> None:
        client = _RecordingClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        await trigger.callback([Delete(id=7)], [None])
        _, body, _ = client.calls[0]
        assert body == [{"kind": "delete", "id": 7}]

    async def test_miss_result_is_none(self) -> None:
        client = _RecordingClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        await trigger.callback([Update(item={"title": "x"})], [None])
        _, body, _ = client.calls[0]
        assert body == [{"kind": "update", "item": {"title": "x"}, "result": None}]

    async def test_dto_instances_are_json_dumped(self) -> None:
        """A real DTO (a pydantic ``BaseModel``), not a plain dict, serializes too."""

        class _Item(BaseModel):
            id: int
            title: str

        client = _RecordingClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        await trigger.callback([Create(item=_Item(id=1, title="a"))], [_Item(id=1, title="a")])
        _, body, _ = client.calls[0]
        assert body == [
            {"kind": "create", "item": {"id": 1, "title": "a"}, "result": {"id": 1, "title": "a"}}
        ]

    async def test_headers_are_sent_with_plaintext_values(self) -> None:
        client = _RecordingClient()
        trigger = WebhookTrigger(
            url="http://x/hook",
            headers=[WebhookHeader(name="X-Secret", value=SecretStr("shh"))],
            client=client,
        )
        await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        _, _, headers = client.calls[0]
        assert headers == {"X-Secret": "shh"}


# ---------------------------------------------------------------------------
# Delivery / retry behavior
# ---------------------------------------------------------------------------


class TestDelivery:
    async def test_single_success_fires_once(self) -> None:
        client = _RecordingClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert len(client.calls) == 1

    async def test_no_retry_raises_immediately_on_failure(self) -> None:
        client = _AlwaysFailsClient()
        trigger = WebhookTrigger(url="http://x/hook", client=client)
        with pytest.raises(RuntimeError, match="status 500"):
            await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert client.calls == 1

    async def test_retries_until_success_within_budget(self) -> None:
        client = _RecordingClient(fail_times=2)
        trigger = WebhookTrigger(
            url="http://x/hook",
            retry=FixedDelayRetry(max_retries=3, delay_seconds=0.001),
            client=client,
        )
        await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert len(client.calls) == 3

    async def test_exhausting_retries_reraises_the_last_error(self) -> None:
        client = _AlwaysFailsClient()
        trigger = WebhookTrigger(
            url="http://x/hook",
            retry=FixedDelayRetry(max_retries=2, delay_seconds=0.001),
            client=client,
        )
        with pytest.raises(RuntimeError, match="status 500"):
            await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert client.calls == 3  # the initial attempt plus both retries

    async def test_bind_client_attaches_after_construction(self) -> None:
        trigger = WebhookTrigger(url="http://x/hook")
        client = _RecordingClient()
        trigger.bind_client(client)
        await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# No-client path (lazy httpx import) — exercised without a real network call
# by asserting the actionable error when httpx cannot be imported.
# ---------------------------------------------------------------------------


class TestLazyImport:
    async def test_missing_httpx_raises_actionable_error(self, monkeypatch: Any) -> None:
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "httpx":
                raise ImportError("No module named 'httpx'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        trigger = WebhookTrigger(url="http://x/hook")
        with pytest.raises(ImportError, match="resourcey\\[webhooks\\]"):
            await trigger.callback([Create(item={"a": 1})], [{"a": 1}])

    async def test_no_injected_client_builds_one_via_the_real_import(
        self, monkeypatch: Any
    ) -> None:
        """Without ``client=``, ``callback`` really does ``import httpx`` and open a client.

        A fake ``httpx`` module (registered in ``sys.modules``, so ``import httpx``
        resolves to it exactly as it would resolve to the real package) proves the
        no-injection branch builds and enters an ``AsyncClient`` itself, rather than
        only ever being exercised via the ``client=`` escape hatch.
        """
        import sys

        calls: list[tuple[str, Any, dict[str, str]]] = []

        class _FakeAsyncClient:
            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> _FakeAsyncClient:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, url: str, json: Any, headers: dict[str, str]) -> _FakeResponse:
                calls.append((url, json, headers))
                return _FakeResponse(200)

        fake_httpx = type("FakeHttpxModule", (), {"AsyncClient": _FakeAsyncClient})
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        trigger = WebhookTrigger(url="http://x/hook", timeout_seconds=5.0)
        await trigger.callback([Create(item={"a": 1})], [{"a": 1}])
        assert calls == [
            ("http://x/hook", [{"kind": "create", "item": {"a": 1}, "result": {"a": 1}}], {})
        ]
