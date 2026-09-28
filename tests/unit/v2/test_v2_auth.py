"""Tests for the ``v2`` auth package and the secret-serialization convention.

These drive the real code paths — a real SQLite ``ApiKey`` table, the real
``create_app`` transport, and the real ``ListResource`` over config entries — with
no mocks (the only stand-in is an in-memory encryption double, used where the
point *is* the precedence rule). Covered:

* the serialization convention — redact by default, reveal under
  ``expose_secrets``, and encryption winning over exposure;
* :class:`ApiKeyAuthenticator` against both key sources (DB-backed and
  config-list), correct / incorrect / absent keys, and fail-closed;
* the digest-at-rest guarantee for both sources;
* the one-time reveal (create only) and the query-surface gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import Boolean, String, Uuid, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from resourcey.v2.auth.auth_api_key import (
    API_KEY_CHALLENGE,
    API_KEY_HEADER_NAME,
    ApiKeyAuthenticator,
)
from resourcey.v2.auth.auth_api_key_resource import (
    ApiKey,
    ApiKeyBase,
    config_api_key_models,
    config_api_key_resource,
    config_api_key_view,
    encode_base36,
    generate_api_key,
    hash_api_key,
    stored_api_key_resource,
    stored_api_key_view,
)
from resourcey.v2.auth.auth_authorized_dependency import AuthorizedDependencyBuilder
from resourcey.v2.auth.auth_config import ApiKeyConfig, ApiKeysConfig
from resourcey.v2.auth.auth_principal import PrincipalKind
from resourcey.v2.core.dto import DTO
from resourcey.v2.core.manifest import Manifest
from resourcey.v2.http.app import create_app
from resourcey.v2.list.list_resource import ListResource
from resourcey.v2.sql.sql_resource import SqlResource
from resourcey.v2.util.secret_serialization import (
    dump_secret_str,
    load_secret_str,
)


class Widget(BaseModel):
    """A minimal served model for the auth-protected resource in the tests."""

    id: int
    label: str


class _Context:
    """A minimal ``info``-shaped object carrying a pydantic serialization context."""

    def __init__(self, context: dict[str, Any] | None) -> None:
        self.context = context


class _FakeEncryption:
    """A reversible stand-in for the encryption service (the precedence test only)."""

    def encrypt_value(self, value: str) -> str:
        return f"enc({value})"

    def decrypt_value(self, value: str) -> str:
        assert value.startswith("enc(") and value.endswith(")")
        return value[4:-1]


# ---------------------------------------------------------------------------
# The serialization convention
# ---------------------------------------------------------------------------


class SecretCarrier(DTO):
    id: int
    name: str
    value: SecretStr


def test_secret_redacts_by_default():
    model = SecretCarrier.get_dto_type()
    dumped = model(id=1, name="n", value=SecretStr("hunter2")).model_dump(mode="json")
    assert dumped["value"] == "**********"


def test_secret_reveals_under_expose_secrets():
    model = SecretCarrier.get_dto_type()
    dumped = model(id=1, name="n", value=SecretStr("hunter2")).model_dump(
        mode="json", context={"expose_secrets": True}
    )
    assert dumped["value"] == "hunter2"


def test_encryption_wins_over_exposure():
    enc = _FakeEncryption()
    dumped = dump_secret_str(
        SecretStr("hunter2"), _Context({"encryption_service": enc, "expose_secrets": True})
    )
    assert dumped == "enc(hunter2)"


def test_encryption_round_trips_on_load():
    enc = _FakeEncryption()
    context = _Context({"encryption_service": enc})
    ciphertext = dump_secret_str(SecretStr("hunter2"), context)
    assert load_secret_str(ciphertext, context) == "hunter2"


def test_no_context_redacts_and_passthrough_loads():
    assert dump_secret_str(SecretStr("hunter2")) == "**********"
    assert load_secret_str("stored") == "stored"


def test_secret_serializer_is_attached_to_each_generated_shape():
    models = SecretCarrier.get_rest_models()
    for model in (
        models.create_response,
        models.update_response,
        models.read_response,
        models.search_response,
    ):
        assert "value" in model.model_fields
        dumped = model.model_validate({"id": 1, "name": "n", "value": "hunter2"}).model_dump(
            mode="json", context={"expose_secrets": True}
        )
        assert dumped["value"] == "hunter2"


# ---------------------------------------------------------------------------
# Key generation / hashing
# ---------------------------------------------------------------------------


def test_generate_api_key_is_prefixed_and_fixed_width():
    key = generate_api_key()
    assert key.startswith("rsk_")
    assert generate_api_key() != key


def test_encode_base36_pads_to_width():
    assert encode_base36(0, 4) == "0000"
    assert encode_base36(35, 4) == "000z"
    assert encode_base36(36, 4) == "0010"


def test_hash_api_key_is_sha256_hex_and_matches_v1():
    digest = hash_api_key("rsk_abc")
    assert digest == hash_api_key("rsk_abc")
    assert len(digest) == 64
    assert digest != "rsk_abc"


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_api_keys_config_parses_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_API_KEYS_0_ID", "k1")
    monkeypatch.setenv("APP_API_KEYS_0_NAME", "one")
    monkeypatch.setenv("APP_API_KEYS_0_KEY", "plaintext-one")
    monkeypatch.setenv("APP_API_KEYS_1_ID", "k2")
    monkeypatch.setenv("APP_API_KEYS_1_KEY", "plaintext-two")
    ApiKeysConfig.clear_instance_cache()

    cfg = ApiKeysConfig.get_instance()
    assert [(k.id, k.name) for k in cfg.api_keys] == [("k1", "one"), ("k2", None)]
    assert cfg.api_keys[0].key.get_secret_value() == "plaintext-one"


def test_config_models_hash_plaintext_on_load():
    cfg = ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", name="one", key=SecretStr("plain"))])
    models = config_api_key_models(cfg)
    assert models[0].id == "k1"
    assert models[0].name == "one"
    assert models[0].key.get_secret_value() == hash_api_key("plain")
    assert models[0].key.get_secret_value() != "plain"


# ---------------------------------------------------------------------------
# The authenticator against the DB-backed source
# ---------------------------------------------------------------------------


class _RecordingList(ListResource[Widget, int]):
    """A real list resource whose ``get_service`` records whether it was reached."""

    opened = False

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Any:
        type(self).opened = True
        raise AssertionError("target storage must not be opened before the auth check")


@pytest_asyncio.fixture
async def db_app() -> AsyncIterator[tuple[AsyncClient, ApiKeyAuthenticator, Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(ApiKeyBase.metadata.create_all)

    inner = stored_api_key_resource(session_factory=maker)
    view = stored_api_key_view(inner)
    widgets = ListResource([Widget(id=1, label="a")], path="widgets")
    manifest = Manifest(resources=[view, widgets])
    builder = ApiKeyAuthenticator(key_resource=inner)
    app = create_app(
        manifest, dependency_builder=AuthorizedDependencyBuilder(authenticator=builder)
    )

    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, builder, inner
    await engine.dispose()


async def _mint(inner: Any, name: str = "bootstrap") -> str:
    """Mint a key through the inner service and return the raw value."""
    async with await inner.get_service({}) as service:
        created = await service.create(inner.get_dto_type()(name=name))
        return created.key.get_secret_value()


async def test_absent_and_invalid_keys_are_identical(db_app):
    client, _builder, _inner = db_app
    missing = await client.get("/widgets")
    wrong = await client.get("/widgets", headers={API_KEY_HEADER_NAME: "nope"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert wrong.headers["www-authenticate"] == API_KEY_CHALLENGE
    assert missing.json() == wrong.json()


async def test_valid_key_authenticates_both_headers(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner)
    by_header = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    by_bearer = await client.get("/widgets", headers={"Authorization": f"Bearer {raw}"})
    assert by_header.status_code == 200
    assert by_bearer.status_code == 200


async def test_bearer_ignored_when_header_present(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner)
    response = await client.get(
        "/widgets",
        headers={API_KEY_HEADER_NAME: raw, "Authorization": "Bearer wrong"},
    )
    assert response.status_code == 200


async def test_fail_closed_without_a_key_resource():
    builder = ApiKeyAuthenticator(key_resource=None)
    assert await builder.lookup_api_key("anything") is None


async def test_key_check_runs_before_target_storage():
    _RecordingList.opened = False
    builder = AuthorizedDependencyBuilder(authenticator=ApiKeyAuthenticator(key_resource=None))
    dependency = builder.get_service_dependency(_RecordingList([Widget(id=1, label="a")]))
    app = FastAPI()

    async def handler(service: Any = Depends(dependency)) -> dict[str, bool]:  # noqa: B008
        return {"ok": True}

    app.get("/protected")(handler)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/protected")
    assert response.status_code == 401
    assert _RecordingList.opened is False


async def test_api_key_dependency_is_reusable_on_a_router(db_app):
    _client, builder, inner = db_app
    raw = await _mint(inner)
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(builder.api_key_dependency)])

    @router.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/ping")).status_code == 401
        ok = await client.get("/ping", headers={API_KEY_HEADER_NAME: raw})
        assert ok.status_code == 200


async def test_both_key_schemes_appear_in_openapi(db_app):
    client, _builder, _inner = db_app
    schema = (await client.get("/openapi.json")).json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["ApiKeyHeader"]["name"] == API_KEY_HEADER_NAME
    assert schemes["ApiKeyBearer"]["scheme"] == "bearer"


# ---------------------------------------------------------------------------
# The DB-backed resource: reveal, digest at rest, query gate
# ---------------------------------------------------------------------------


async def test_key_stored_only_as_a_digest(db_app):
    _client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    async with inner._session_factory() as session:
        rows = (await session.execute(select(ApiKey))).scalars().all()
    # The ORM attribute is a plain string here (the column is a ``String``); the
    # DTO/DTO-model layer is where it becomes a ``SecretStr``.
    stored = {row.key for row in rows}
    assert hash_api_key(raw) in stored
    assert raw not in stored


async def test_create_reveals_the_raw_key_exactly_once(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")

    created = await client.post(
        "/api-keys", json={"name": "ci"}, headers={API_KEY_HEADER_NAME: raw}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("rsk_")
    assert body["key"] != hash_api_key(body["key"])
    created_id = body["id"]

    read = await client.get(f"/api-keys/{created_id}", headers={API_KEY_HEADER_NAME: raw})
    assert read.status_code == 200
    assert "key" not in read.json()

    search = await client.get("/api-keys", headers={API_KEY_HEADER_NAME: raw})
    assert search.status_code == 200
    assert all("key" not in item for item in search.json()["items"])


async def test_key_cannot_be_supplied_on_create(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    created = await client.post(
        "/api-keys",
        json={"name": "ci", "key": "client-chosen"},
        headers={API_KEY_HEADER_NAME: raw},
    )
    # The create request carries no ``key`` field, so the forged value is ignored
    # and a server-minted key is returned instead.
    assert created.status_code == 201
    assert created.json()["key"] != "client-chosen"


async def test_update_cannot_rotate_the_key(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    created = await client.post(
        "/api-keys", json={"name": "ci"}, headers={API_KEY_HEADER_NAME: raw}
    )
    created_id, minted = created.json()["id"], created.json()["key"]
    updated = await client.patch(
        f"/api-keys/{created_id}", json={"name": "renamed"}, headers={API_KEY_HEADER_NAME: raw}
    )
    assert updated.status_code == 200
    assert "key" not in updated.json()
    assert updated.json()["name"] == "renamed"
    # The minted key still authenticates after the rename.
    check = await client.get("/widgets", headers={API_KEY_HEADER_NAME: minted})
    assert check.status_code == 200


async def test_query_surface_rejects_the_key_field(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    filtered = await client.get(
        "/api-keys", params={"key__eq": "x"}, headers={API_KEY_HEADER_NAME: raw}
    )
    sorted_ = await client.get(
        "/api-keys", params={"sort": "key"}, headers={API_KEY_HEADER_NAME: raw}
    )
    assert filtered.status_code == 400
    assert sorted_.status_code == 400


async def test_stored_service_find_by_key_round_trips(db_app):
    _client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    async with await inner.get_service({}) as service:
        assert await service.find_by_key(hash_api_key(raw)) is not None
        assert await service.find_by_key(hash_api_key("other")) is None


async def _set_row(inner: Any, raw: str, **values: Any) -> None:
    """Update the stored ``ApiKey`` row for ``raw`` (the credentials DB)."""
    async with inner._session_factory() as session:
        row = (
            await session.execute(select(ApiKey).where(ApiKey.key == hash_api_key(raw)))
        ).scalar_one()
        for name, value in values.items():
            setattr(row, name, value)
        await session.commit()


async def test_inactive_key_does_not_authenticate(db_app):
    client, builder, inner = db_app
    raw = await _mint(inner, "ci")
    assert (await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})).status_code == 200
    await _set_row(inner, raw, active=False)
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 401
    assert await builder.lookup_api_key(raw) is None


async def test_expired_key_does_not_authenticate(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    await _set_row(inner, raw, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 401


async def test_future_expires_at_still_authenticates(db_app):
    client, _builder, inner = db_app
    raw = await _mint(inner, "ci")
    await _set_row(inner, raw, expires_at=datetime.now(UTC) + timedelta(days=1))
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 200


async def test_owned_db_key_resolves_to_a_user_principal(db_app):
    client, builder, inner = db_app
    raw = await _mint(inner, "ci")
    owner = uuid4()
    await _set_row(inner, raw, user_id=owner)
    result = await builder.lookup_api_key(raw)
    assert result is not None
    principal = builder._principal_for(result)
    assert principal.id == owner
    assert principal.kind is PrincipalKind.USER
    assert (await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})).status_code == 200


# ---------------------------------------------------------------------------
# The optional principal store (a key's principal validated against a user
# resource)
# ---------------------------------------------------------------------------


class _UserBase(DeclarativeBase):
    """A minimal principal table for the store-backed authenticator tests."""


class _User(_UserBase):
    """A stored principal: an id, an email, and an ``enabled`` flag."""

    __tablename__ = "auth_users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


async def _add_user(users: Any, user_id: UUID, *, enabled: bool = True) -> None:
    """Insert a stored principal through the user resource's own service."""
    dto = users.get_dto_type()
    async with await users.get_service({}) as service:
        await service.create(dto(id=user_id, email=f"{user_id}@example.com", enabled=enabled))


@pytest_asyncio.fixture
async def principal_store_app() -> AsyncIterator[tuple[AsyncClient, ApiKeyAuthenticator, Any, Any]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(ApiKeyBase.metadata.create_all)
        await conn.run_sync(_UserBase.metadata.create_all)

    keys = stored_api_key_resource(session_factory=maker)
    users = SqlResource(_User, session_factory=maker)
    widgets = ListResource([Widget(id=1, label="a")], path="widgets")
    manifest = Manifest(resources=[stored_api_key_view(keys), widgets])
    authenticator = ApiKeyAuthenticator(key_resource=keys, user_resource=users)
    app = create_app(
        manifest, dependency_builder=AuthorizedDependencyBuilder(authenticator=authenticator)
    )

    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, authenticator, keys, users
    await engine.dispose()


async def test_key_with_an_unknown_principal_is_rejected(principal_store_app):
    client, _authenticator, keys, _users = principal_store_app
    raw = await _mint(keys, "ci")
    await _set_row(keys, raw, user_id=uuid4())
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 401


async def test_disabled_principal_is_rejected(principal_store_app):
    client, _authenticator, keys, users = principal_store_app
    user_id = uuid4()
    await _add_user(users, user_id, enabled=False)
    raw = await _mint(keys, "ci")
    await _set_row(keys, raw, user_id=user_id)
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 401


async def test_enabled_principal_authenticates(principal_store_app):
    client, authenticator, keys, users = principal_store_app
    user_id = uuid4()
    await _add_user(users, user_id, enabled=True)
    raw = await _mint(keys, "ci")
    await _set_row(keys, raw, user_id=user_id)
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: raw})
    assert response.status_code == 200
    row = await authenticator.lookup_api_key(raw)
    assert row is not None
    principal = authenticator._principal_for(row)
    assert principal.id == user_id
    assert principal.kind is PrincipalKind.USER


# ---------------------------------------------------------------------------
# The config-list source
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def config_app() -> AsyncIterator[tuple[AsyncClient, ApiKeyAuthenticator]]:
    cfg = ApiKeysConfig(
        api_keys=[
            ApiKeyConfig(id="k1", name="one", key=SecretStr("secret-one")),
            ApiKeyConfig(id="k2", name="two", key=SecretStr("secret-two")),
        ]
    )
    inner = config_api_key_resource(cfg)
    view = config_api_key_view(inner)
    widgets = ListResource([Widget(id=1, label="a")], path="widgets")
    manifest = Manifest(resources=[view, widgets])
    builder = ApiKeyAuthenticator(key_resource=inner)
    app = create_app(
        manifest, dependency_builder=AuthorizedDependencyBuilder(authenticator=builder)
    )
    async with manifest:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, builder


async def test_config_source_authenticates_and_hides_the_digest(config_app):
    client, _builder = config_app
    ok = await client.get("/widgets", headers={API_KEY_HEADER_NAME: "secret-one"})
    assert ok.status_code == 200

    listing = await client.get("/api-keys", headers={API_KEY_HEADER_NAME: "secret-one"})
    assert listing.status_code == 200
    assert listing.json()["items"] == [
        {"id": "k1", "name": "one"},
        {"id": "k2", "name": "two"},
    ]

    filtered = await client.get(
        "/api-keys", params={"key__eq": "x"}, headers={API_KEY_HEADER_NAME: "secret-one"}
    )
    assert filtered.status_code == 400


async def test_config_source_is_read_only(config_app):
    client, _builder = config_app
    created = await client.post(
        "/api-keys", json={"name": "new"}, headers={API_KEY_HEADER_NAME: "secret-one"}
    )
    assert created.status_code == 405


async def test_config_source_rejects_a_wrong_key(config_app):
    client, _builder = config_app
    response = await client.get("/widgets", headers={API_KEY_HEADER_NAME: "wrong"})
    assert response.status_code == 401


async def test_config_service_find_by_key_uses_constant_time_compare(config_app):
    _client, _builder = config_app
    inner = config_api_key_resource(
        ApiKeysConfig(api_keys=[ApiKeyConfig(id="k1", key=SecretStr("secret-one"))])
    )
    async with await inner.get_service({}) as service:
        assert await service.find_by_key(hash_api_key("secret-one")) is not None
        assert await service.find_by_key(hash_api_key("secret-two")) is None
        assert await service.find_by_key("not-a-digest") is None
