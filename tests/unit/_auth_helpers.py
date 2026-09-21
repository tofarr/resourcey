"""Shared test helpers for auth coverage tests."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from resourcey.auth.auth_models import User
from resourcey.auth.auth_service import AuthService
from resourcey.auth.password import hash_password


async def make_user(
    session: AsyncSession,
    *,
    email: str = "test@example.com",
    username: str = "test",
    password: str = "s3cret",
    enabled: bool = True,
    idp_user_id: str | None = None,
) -> User:
    user = User(
        email=email,
        username=username,
        password=hash_password(password),
        enabled=enabled,
        idp_user_id=idp_user_id,
    )
    session.add(user)
    await session.flush()
    return user


def make_id_token(sub: str, email: str) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none"}).encode())
    payload = b64(json.dumps({"sub": sub, "email": email}).encode())
    return f"{header}.{payload}."


def persist_idp_tokens(auth_service: AuthService, user_id: uuid.UUID, **overrides: Any) -> Any:
    idp_tokens: dict[str, Any] = {
        "access_token": "idp-access",
        "refresh_token": "idp-refresh",
        "expires_in": 3600,
        "refresh_expires_in": 86400,
        "id_token": make_id_token(str(user_id), "test@example.com"),
    }
    idp_tokens.update(overrides)
    return auth_service.persist_idp_tokens(user_id, idp_tokens)


class MockResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class MockIdpClient:
    """A minimal mock httpx.AsyncClient for the IdP token endpoint."""

    def __init__(self, *, token_response: dict[str, Any] | None = None, status: int = 200):
        self._token_response = token_response or {}
        self._status = status
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, data: dict[str, str] | None = None) -> MockResponse:
        self.calls.append({"url": url, "data": data or {}})
        return MockResponse(self._status, self._token_response)

    async def aclose(self) -> None:
        pass
