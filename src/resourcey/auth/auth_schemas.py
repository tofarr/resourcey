"""Pydantic schemas for the auth feature (issue #4).

Ported from ohev2's ``auth_schemas.py``, adapted to resourcey.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DevLoginRequest(BaseModel):
    """Body for ``POST /auth/dev/login``."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"username": "dev-user", "password": "dev-pass"}}
    )

    username: str = Field(description="Username of an enabled local user.")
    password: str = Field(description="The user's password.")


class TokenRequest(BaseModel):
    """Body for ``POST /auth/token`` (RFC 6749 §4.1.3)."""

    grant_type: str = Field(description="'authorization_code' or 'refresh_token'.")
    code: str | None = Field(default=None, description="Authorization code (auth code grant).")
    redirect_uri: str | None = Field(default=None, description="Must match the authorize request.")
    client_id: str
    client_secret: str
    code_verifier: str | None = Field(default=None, description="PKCE code verifier.")
    refresh_token: str | None = Field(default=None, description="Refresh token (refresh grant).")


class TokenResponse(BaseModel):
    """OAuth2 token response (RFC 6749 §5.1)."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    expires_at: datetime = Field(description="Absolute access-token expiry (drift-adjusted).")
    refresh_token_expires_in: int = Field(description="Refresh-token lifetime in seconds.")
    refresh_token_expires_at: datetime = Field(
        description="Absolute refresh-token expiry (drift-adjusted)."
    )
    id_token: str | None = Field(default=None, description="Optional id_token passthrough.")


class UserInfoResponse(BaseModel):
    """OIDC UserInfo response (OIDC Core §5.3)."""

    sub: str
    email: str | None = Field(default=None)
    email_verified: bool | None = Field(default=None)
    name: str | None = Field(default=None)
    preferred_username: str | None = Field(default=None)
