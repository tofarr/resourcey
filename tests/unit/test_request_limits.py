"""Tests for the read surface's request caps (issue #175).

Two client-supplied knobs are bounded at the transport: the ``limit`` on a
``search`` (``RequestLimitsConfig.max_search_limit``) and the number of ids in
one ``batch_read`` (``max_batch_read_ids``). Both default to 100 and are read
once at registration.

These tests drive the real ``create_app`` + ``httpx.ASGITransport`` path over a
``ListResource``, so no storage is involved and every assertion is on behaviour
a client can observe. ``setenv`` / ``clear_instance_cache`` (never a mock) is how
a non-default cap is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from resourcey.config.config_base import _reset_config_prefix
from resourcey.core.errors import ResourceyConfigError
from resourcey.core.manifest import Manifest
from resourcey.http.app import create_app
from resourcey.http.http_config import (
    DEFAULT_MAX_BATCH_READ_IDS,
    DEFAULT_MAX_SEARCH_LIMIT,
    RequestLimitsConfig,
)
from resourcey.list.list_resource import ListResource

ROWS = 200


class Widget(BaseModel):
    id: int
    name: str


def _widgets() -> list[Widget]:
    return [Widget(id=i, name=f"w{i}") for i in range(1, ROWS + 1)]


def _app() -> tuple[Manifest, FastAPI]:
    """A manifest + app serving ``widgets``; registration reads the caps here."""
    manifest = Manifest(resources=[ListResource(_widgets(), path="widgets")])
    return manifest, create_app(manifest)


async def _client() -> AsyncIterator[AsyncClient]:
    manifest, app = _app()
    async with (
        manifest,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start and end every case at the defaults with no cached instance."""
    _reset_config_prefix()
    monkeypatch.delenv("APP_MAX_SEARCH_LIMIT", raising=False)
    monkeypatch.delenv("APP_MAX_BATCH_READ_IDS", raising=False)
    RequestLimitsConfig.clear_instance_cache()
    yield
    RequestLimitsConfig.clear_instance_cache()
    _reset_config_prefix()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async for c in _client():
        yield c


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestRequestLimitsConfig:
    def test_defaults_are_one_hundred_each(self) -> None:
        config = RequestLimitsConfig.get_instance()
        assert config.max_search_limit == DEFAULT_MAX_SEARCH_LIMIT == 100
        assert config.max_batch_read_ids == DEFAULT_MAX_BATCH_READ_IDS == 100

    def test_env_vars_override_each_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_MAX_SEARCH_LIMIT", "7")
        monkeypatch.setenv("APP_MAX_BATCH_READ_IDS", "3")
        config = RequestLimitsConfig.get_instance()
        assert config.max_search_limit == 7
        assert config.max_batch_read_ids == 3

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_cap_below_one_is_rejected_at_build_time(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("APP_MAX_SEARCH_LIMIT", value)
        with pytest.raises(ResourceyConfigError):
            RequestLimitsConfig.get_instance()

    def test_batch_read_cap_below_one_is_rejected_at_build_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_MAX_BATCH_READ_IDS", "0")
        with pytest.raises(ResourceyConfigError):
            RequestLimitsConfig.get_instance()

    def test_none_is_not_an_unlimited_spelling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # FileStoreConfig.max_size treats None as "unlimited"; here that would
        # reopen the unbounded surface the cap exists to close.
        monkeypatch.setenv("APP_MAX_SEARCH_LIMIT", "None")
        with pytest.raises(ResourceyConfigError):
            RequestLimitsConfig.get_instance()

    def test_env_template_documents_both_caps(self) -> None:
        template = RequestLimitsConfig().generate_env_template()
        assert "APP_MAX_SEARCH_LIMIT=100" in template
        assert "APP_MAX_BATCH_READ_IDS=100" in template


# ---------------------------------------------------------------------------
# Default cap — search
# ---------------------------------------------------------------------------


class TestSearchLimitAtTheDefaultCap:
    async def test_an_omitted_limit_uses_the_page_size_default(self, client: AsyncClient) -> None:
        response = await client.get("/widgets")
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 20
        assert len(body["items"]) == 20

    async def test_exactly_the_cap_succeeds(self, client: AsyncClient) -> None:
        response = await client.get("/widgets", params={"limit": 100})
        assert response.status_code == 200
        assert response.json()["limit"] == 100
        assert len(response.json()["items"]) == 100

    async def test_one_over_the_cap_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/widgets", params={"limit": 101})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"
        assert "100" in response.json()["error"]["message"]

    @pytest.mark.parametrize("limit", [0, -1])
    async def test_a_sub_one_limit_is_rejected(self, client: AsyncClient, limit: int) -> None:
        response = await client.get("/widgets", params={"limit": limit})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    async def test_a_huge_limit_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/widgets", params={"limit": 10_000_000})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"


# ---------------------------------------------------------------------------
# Default cap — batch_read
# ---------------------------------------------------------------------------


class TestBatchReadAtTheDefaultCap:
    async def test_exactly_the_cap_succeeds_and_stays_aligned(self, client: AsyncClient) -> None:
        ids = list(range(1, 101))
        response = await client.get("/widgets/batch-read", params={"id": ids})
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == ids

    async def test_one_over_the_cap_is_rejected_not_truncated(self, client: AsyncClient) -> None:
        response = await client.get("/widgets/batch-read", params={"id": list(range(1, 102))})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"
        # A rejected request has no result body at all — never a truncated,
        # misaligned list.
        assert "error" in response.json()

    async def test_a_miss_keeps_its_position(self, client: AsyncClient) -> None:
        response = await client.get("/widgets/batch-read", params={"id": [1, 99_999, 2]})
        assert response.status_code == 200
        body = response.json()
        assert [None if item is None else item["id"] for item in body] == [1, None, 2]


# ---------------------------------------------------------------------------
# A configured non-default cap
# ---------------------------------------------------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch, *, search: int, batch: int) -> None:
    monkeypatch.setenv("APP_MAX_SEARCH_LIMIT", str(search))
    monkeypatch.setenv("APP_MAX_BATCH_READ_IDS", str(batch))
    RequestLimitsConfig.clear_instance_cache()


class TestConfiguredCap:
    async def test_search_boundary_follows_the_configured_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch, search=5, batch=100)
        async for client in _client():
            assert (await client.get("/widgets", params={"limit": 5})).status_code == 200
            assert (await client.get("/widgets", params={"limit": 6})).status_code == 400

    async def test_an_omitted_limit_is_clamped_to_a_cap_below_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEFAULT_LIMIT is 20; with a cap of 5 an omitted limit must still be
        # served (resolved to min(20, 5) at registration) — not rejected.
        _configure(monkeypatch, search=5, batch=100)
        async for client in _client():
            response = await client.get("/widgets")
            assert response.status_code == 200
            assert response.json()["limit"] == 5
            assert len(response.json()["items"]) == 5

    async def test_batch_read_boundary_follows_the_configured_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch, search=100, batch=3)
        async for client in _client():
            assert (
                await client.get("/widgets/batch-read", params={"id": [1, 2, 3]})
            ).status_code == 200
            assert (
                await client.get("/widgets/batch-read", params={"id": [1, 2, 3, 4]})
            ).status_code == 400

    async def test_the_caps_are_read_once_at_registration_not_per_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch, search=100, batch=3)
        manifest, app = _app()
        # Move the caps after registration; the live routes must not move with
        # them, or the OpenAPI document would disagree with the behaviour.
        _configure(monkeypatch, search=1, batch=1)
        async with (
            manifest,
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert (
                await client.get("/widgets/batch-read", params={"id": [1, 2, 3]})
            ).status_code == 200
            assert (await client.get("/widgets", params={"limit": 100})).status_code == 200


# ---------------------------------------------------------------------------
# The bound is documented in OpenAPI
# ---------------------------------------------------------------------------


class TestOpenApiDocumentsTheBound:
    def _search_limit_param(self, app: FastAPI) -> dict[str, Any]:
        params = app.openapi()["paths"]["/widgets"]["get"]["parameters"]
        return next(p for p in params if p["name"] == "limit")

    def _batch_read_id_param(self, app: FastAPI) -> dict[str, Any]:
        params = app.openapi()["paths"]["/widgets/batch-read"]["get"]["parameters"]
        return next(p for p in params if p["name"] == "id")

    def test_search_documents_minimum_and_maximum(self) -> None:
        _, app = _app()
        schema = self._search_limit_param(app)["schema"]
        assert schema["minimum"] == 1
        assert schema["maximum"] == 100
        assert schema["default"] == 20

    def test_batch_read_documents_max_items(self) -> None:
        _, app = _app()
        assert self._batch_read_id_param(app)["schema"]["maxItems"] == 100

    def test_a_configured_cap_reaches_the_document(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch, search=5, batch=3)
        _, app = _app()
        search = self._search_limit_param(app)["schema"]
        assert (search["minimum"], search["maximum"], search["default"]) == (1, 5, 5)
        assert self._batch_read_id_param(app)["schema"]["maxItems"] == 3
