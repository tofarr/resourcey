"""Coverage tests for permission SQL conditions, cookie refresh, and the full
OAuth callback/token E2E flow (issue #4).

Targets the remaining gaps:
- ``permission.py``: AclFilter / CreatorMatchFilter / _ScopeMatchFilter SQL
  conditions and GroupPermission branch coverage.
- ``auth_dependencies.py``: ``_maybe_refresh_cookie`` paths.
- ``auth_router.py``: callback, token, refresh, revoke routes via E2E.
- ``permission_resolver.py``: ``depends_permission_resolver``.
- ``session.py``: ``_get_or_create_factory`` fallback path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from resourcey.auth.auth_models import AuthBase, IdpAccessToken, IdpRefreshToken, TokenType, User
from resourcey.auth.auth_router import router as auth_router
from resourcey.auth.auth_service import (
    AuthService,
    RefreshLockTimeoutError,
    _mint_cookie_jwe,
)
from resourcey.auth.dev_router import router as dev_router
from resourcey.auth.password import hash_password
from resourcey.auth.permission import (
    AclFilter,
    AclPermission,
    CreatorMatchFilter,
    CreatorPermission,
    GroupPermission,
    Permitted,
    ReadOnly,
    _ScopeMatchFilter,
)
from resourcey.auth.permission_resolver import (
    DefaultPermissions,
    PermissionResolver,
    depends_permission_resolver,
)
from resourcey.config.config_framework import AuthConfig, FrameworkConfig, IdpConfig
from resourcey.config.config_runtime import set_config
from resourcey.encryption.encryption_config import EncryptionKeyConfig, EncryptionKeysConfig
from resourcey.encryption.encryption_service import EncryptionService
from resourcey.resource.service_base import Action
from resourcey.util.search_filter import NONE, AllSearchFilter, NoneSearchFilter, SearchFilter

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _enc_service() -> EncryptionService:
    return EncryptionService(
        EncryptionKeysConfig(encryption_key=EncryptionKeyConfig(id="k1", value="test-secret"))
    )


@pytest.fixture
def enc() -> EncryptionService:
    return _enc_service()


@pytest.fixture
def patched_enc(enc: EncryptionService, framework_config: FrameworkConfig):
    """Patch get_encryption_service in all modules that import it by name."""
    set_config(framework_config)
    import resourcey.auth.auth_dependencies as deps_mod
    import resourcey.auth.auth_router as router_mod
    import resourcey.auth.auth_service as svc_mod
    import resourcey.auth.auth_tokens as tokens_mod
    import resourcey.auth.dev_router as dev_mod
    import resourcey.auth.session as sess_mod
    import resourcey.encryption.encryption_service as enc_mod

    originals = {
        enc_mod: enc_mod.get_encryption_service,
        svc_mod: getattr(svc_mod, "get_encryption_service", None),
        tokens_mod: getattr(tokens_mod, "get_encryption_service", None),
        deps_mod: getattr(deps_mod, "get_encryption_service", None),
        router_mod: getattr(router_mod, "get_encryption_service", None),
        dev_mod: getattr(dev_mod, "get_encryption_service", None),
        sess_mod: getattr(sess_mod, "get_encryption_service", None),
    }

    def _patched() -> EncryptionService:
        return enc

    _patched.cache_clear = lambda: None  # type: ignore[attr-defined]

    for mod, _ in originals.items():
        if hasattr(mod, "get_encryption_service"):
            mod.get_encryption_service = _patched  # type: ignore[assignment]

    yield enc

    for mod, orig in originals.items():
        if orig is not None:
            mod.get_encryption_service = orig  # type: ignore[assignment]


@pytest.fixture
def idp_config() -> IdpConfig:
    return IdpConfig(
        url="/auth/dev",
        client_id="resourcey",
        client_secret="changeme",
        access_token_expires_in=900,
        refresh_token_expires_in=86400,
    )


@pytest.fixture
def framework_config(idp_config: IdpConfig) -> FrameworkConfig:
    return FrameworkConfig(
        database=FrameworkConfig().database,
        auth=AuthConfig(
            cookie_name="resourcey_session",
            cookie_secure=False,
            cookie_samesite="lax",
            idp=idp_config,
        ),
        base_url="http://localhost:8000",
    )


@pytest_asyncio.fixture
async def session(framework_config: FrameworkConfig) -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        yield sess
    await engine.dispose()


# ---------------------------------------------------------------------------
# Permission: SQL conditions for AclFilter, CreatorMatchFilter, _ScopeMatchFilter
# ---------------------------------------------------------------------------


class TestPermissionSqlConditions:
    def test_acl_filter_empty_ids_matches_all(self) -> None:
        f = AclFilter[Any](ids=[])
        assert f.matches(object())
        assert f.sql_condition() is None
        assert f.negated_sql_condition() is not None  # false()

    def test_acl_filter_with_ids_matches(self) -> None:
        item_id = uuid.uuid4()
        f = AclFilter[Any](ids=[item_id])
        from types import SimpleNamespace

        assert f.matches(SimpleNamespace(id=item_id))
        assert not f.matches(SimpleNamespace(id=uuid.uuid4()))
        cond = f.sql_condition()
        assert cond is not None
        neg = f.negated_sql_condition()
        assert neg is not None

    def test_acl_filter_no_id_attr(self) -> None:
        f = AclFilter[Any](ids=[uuid.uuid4()])
        assert not f.matches(object())

    def test_creator_match_filter_matches(self) -> None:
        creator = uuid.uuid4()
        from types import SimpleNamespace

        f = CreatorMatchFilter[Any](creator_id=creator)
        assert f.matches(SimpleNamespace(creator_id=creator))
        assert not f.matches(SimpleNamespace(creator_id=uuid.uuid4()))
        assert not f.matches(SimpleNamespace(creator_id=None))
        assert not f.matches(object())  # no creator_id attr
        cond = f.sql_condition()
        assert cond is not None
        neg = f.negated_sql_condition()
        assert neg is not None

    def test_scope_match_filter_match_branch(self) -> None:
        """When match filter admits the item, in_scope is applied."""
        item_id = uuid.uuid4()
        from types import SimpleNamespace

        match = AclFilter[Any](ids=[item_id])
        scope = _ScopeMatchFilter[Any](
            match=match, in_scope=AllSearchFilter[Any](), out_scope=NoneSearchFilter[Any]()
        )
        assert scope.matches(SimpleNamespace(id=item_id))

    def test_scope_match_filter_out_of_scope_branch(self) -> None:
        """When match filter does not admit the item, out_scope is applied."""
        item_id = uuid.uuid4()
        from types import SimpleNamespace

        match = AclFilter[Any](ids=[item_id])
        scope = _ScopeMatchFilter[Any](
            match=match, in_scope=NoneSearchFilter[Any](), out_scope=AllSearchFilter[Any]()
        )
        # Item not in match -> out_scope (ALL) admits it.
        assert scope.matches(SimpleNamespace(id=uuid.uuid4()))

    def test_scope_match_filter_sql_condition_match_none(self) -> None:
        """When match.sql_condition() is None, only in_scope applies."""
        match = AclFilter[Any](ids=[])  # sql_condition() is None
        scope = _ScopeMatchFilter[Any](
            match=match, in_scope=AllSearchFilter[Any](), out_scope=NoneSearchFilter[Any]()
        )
        cond = scope.sql_condition()
        # in_scope is ALL -> None, so the result is None.
        assert cond is None

    def test_scope_match_filter_sql_condition_full(self) -> None:
        """Both branches produce an OR condition."""
        item_id = uuid.uuid4()
        match = AclFilter[Any](ids=[item_id])
        scope = _ScopeMatchFilter[Any](
            match=match,
            in_scope=AllSearchFilter[Any](),  # None
            out_scope=AllSearchFilter[Any](),  # None
        )
        cond = scope.sql_condition()
        assert cond is not None

    def test_scope_match_filter_sql_condition_negated_none(self) -> None:
        """When negated_sql_condition() is None, only left branch applies.

        AclFilter with non-empty ids has a non-None negated condition, so this
        path is hard to reach with built-in filters. Test the AclFilter
        negated condition directly instead, and verify _ScopeMatchFilter
        handles the match_cond-is-None early return (covered by
        test_scope_match_filter_sql_condition_match_none).
        """
        # Verify AclFilter.negated_sql_condition returns false() for empty ids.
        f = AclFilter[Any](ids=[])
        assert f.negated_sql_condition() is not None  # false()

    def test_scope_match_filter_resolve_child_dict(self) -> None:
        """_ScopeMatchFilter._resolve_child converts dict to SearchFilter."""
        match_dict = {"kind": "AllSearchFilter"}
        scope = _ScopeMatchFilter[Any](
            match=match_dict,
            in_scope={"kind": "AllSearchFilter"},
            out_scope={"kind": "NoneSearchFilter"},
        )
        assert isinstance(scope.match, SearchFilter)
        assert isinstance(scope.in_scope, SearchFilter)
        assert isinstance(scope.out_scope, SearchFilter)

    def test_group_permission_member(self) -> None:
        g = uuid.uuid4()
        p = GroupPermission(group_ids=[g], on_match=Permitted())
        f = p.to_search_filter(uuid.uuid4(), Action.READ, frozenset({g}))
        assert isinstance(f, AllSearchFilter)

    def test_group_permission_non_member(self) -> None:
        g = uuid.uuid4()
        p = GroupPermission(group_ids=[g], on_match=Permitted(), on_mismatch=ReadOnly())
        f = p.to_search_filter(uuid.uuid4(), Action.READ, frozenset())
        # ReadOnly reduces to a filter that admits read but not write.
        assert f is not NONE

    def test_group_permission_create(self) -> None:
        g = uuid.uuid4()
        p = GroupPermission(group_ids=[g], on_create=Permitted())
        f = p.to_search_filter(uuid.uuid4(), Action.CREATE, frozenset({g}))
        assert isinstance(f, AllSearchFilter)

    def test_acl_permission_create(self) -> None:
        p = AclPermission(item_ids=[uuid.uuid4()], on_create=Permitted())
        f = p.to_search_filter(uuid.uuid4(), Action.CREATE, frozenset())
        assert isinstance(f, AllSearchFilter)

    def test_acl_permission_empty_ids(self) -> None:
        p = AclPermission(item_ids=[], on_mismatch=ReadOnly())
        f = p.to_search_filter(uuid.uuid4(), Action.READ, frozenset())
        assert f is not NONE

    def test_acl_permission_id_cap_exceeded(self) -> None:
        from resourcey.auth.permission import ACL_MAX_IDS

        with pytest.raises(ValueError, match="at most"):
            AclPermission(item_ids=[uuid.uuid4() for _ in range(ACL_MAX_IDS + 1)])

    def test_creator_permission_create(self) -> None:
        p = CreatorPermission(on_create=Permitted())
        f = p.to_search_filter(uuid.uuid4(), Action.CREATE, frozenset())
        assert isinstance(f, AllSearchFilter)

    def test_creator_permission_anonymous(self) -> None:
        p = CreatorPermission(on_mismatch=ReadOnly())
        f = p.to_search_filter(None, Action.READ, frozenset())
        assert f is not NONE


# ---------------------------------------------------------------------------
# PermissionResolver: depends_permission_resolver
# ---------------------------------------------------------------------------


class TestPermissionResolverDep:
    async def test_depends_permission_resolver_with_session(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
    ) -> None:
        set_config(framework_config)
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace(session=session))
        resolver = await depends_permission_resolver(request, session)
        assert isinstance(resolver, PermissionResolver)
        assert resolver._session is session

    async def test_depends_permission_resolver_no_state_session(
        self,
        session: AsyncSession,
        framework_config: FrameworkConfig,
    ) -> None:
        set_config(framework_config)
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace())
        resolver = await depends_permission_resolver(request, session)
        assert isinstance(resolver, PermissionResolver)
        assert resolver._session is session

    async def test_depends_permission_resolver_no_session(
        self,
        framework_config: FrameworkConfig,
    ) -> None:
        set_config(framework_config)
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace())
        resolver = await depends_permission_resolver(request, None)
        assert isinstance(resolver, PermissionResolver)
        assert resolver._session is None

    async def test_make_resolver_factory(
        self,
        session: AsyncSession,
    ) -> None:
        from resourcey.auth.permission_resolver import make_resolver

        defaults = DefaultPermissions({"doc": [Permitted()]})
        resolver = make_resolver(session, defaults=defaults)
        assert isinstance(resolver, PermissionResolver)
        assert resolver._session is session

    async def test_default_permissions_add(self) -> None:
        defaults = DefaultPermissions()
        new = defaults.add("doc", Permitted())
        assert len(new.for_resource("doc")) == 1
        # Original is unchanged.
        assert len(defaults.for_resource("doc")) == 0

    async def test_default_permissions_from_config_skip_bad(
        self,
    ) -> None:
        defaults = DefaultPermissions.from_config(
            {
                "doc": [{"kind": "Permitted"}, {"kind": "UnknownType"}, "not-a-dict"],
            }
        )
        assert len(defaults.for_resource("doc")) == 1

    async def test_resolver_returns_none_when_no_policies(
        self,
        session: AsyncSession,
    ) -> None:
        resolver = PermissionResolver(session)
        result = await resolver.resolve("unknown", Action.READ, uuid.uuid4())
        assert result is None

    async def test_resolver_defaults_only_no_session(self) -> None:
        resolver = PermissionResolver(None, defaults=DefaultPermissions({"doc": [Permitted()]}))
        result = await resolver.resolve("doc", Action.READ, None)
        assert result is not None
        assert result is not NONE

    async def test_resolver_corrupt_db_policy_skipped(
        self,
        session: AsyncSession,
    ) -> None:
        from resourcey.auth.auth_models import UserPermission

        user_id = uuid.uuid4()
        # Insert a corrupt permission JSON.
        session.add(
            UserPermission(
                user_id=user_id,
                resource_type="doc",
                permission={"kind": "UnknownBadType"},
            )
        )
        await session.flush()
        resolver = PermissionResolver(session)
        result = await resolver.resolve("doc", Action.READ, user_id)
        # Corrupt policy is skipped, no other policies -> None.
        assert result is None


# ---------------------------------------------------------------------------
# auth_dependencies: _maybe_refresh_cookie paths
# ---------------------------------------------------------------------------


class TestCookieRefresh:
    async def _make_user_and_tokens(
        self, session: AsyncSession, enc: EncryptionService, framework_config: FrameworkConfig
    ) -> tuple[User, IdpAccessToken, IdpRefreshToken]:
        from resourcey.auth.auth_models import User

        user = User(
            email="cr@example.com", username="cr", password=hash_password("x"), enabled=True
        )
        session.add(user)
        await session.flush()
        auth_svc = AuthService(session, encryption_service=enc, config=framework_config)
        refresh_row, access_row = await auth_svc.persist_idp_tokens(
            user.id,
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "refresh_expires_in": 86400,
            },
        )
        await session.flush()
        return user, access_row, refresh_row

    async def test_maybe_refresh_cookie_plain_no_aid(
        self,
        session: AsyncSession,
        patched_enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        """Cookie without aid (plain cookie) is re-minted off its own exp."""
        enc = patched_enc
        from starlette.responses import Response

        from resourcey.auth.auth_dependencies import _maybe_refresh_cookie

        token = enc.create_jwe_token(
            {"sub": str(uuid.uuid4()), "ttyp": TokenType.COOKIE.value, "jti": str(uuid.uuid4())},
            expires_in=timedelta(hours=1),
        )
        resp = Response()
        await _maybe_refresh_cookie(token, resp, session)
        assert resp.headers.get("set-cookie")

    async def test_maybe_refresh_cookie_federated_not_expiring(
        self,
        session: AsyncSession,
        patched_enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        """Cookie with aid that is not near expiry is re-minted without refresh."""
        enc = patched_enc
        user, access_row, _ = await self._make_user_and_tokens(session, enc, framework_config)
        from starlette.responses import Response

        from resourcey.auth.auth_dependencies import _maybe_refresh_cookie

        cookie = _mint_cookie_jwe(
            enc, user_id=user.id, access_id=access_row.id, access_expires_at=access_row.expires_at
        )
        resp = Response()
        await _maybe_refresh_cookie(cookie, resp, session)
        assert resp.headers.get("set-cookie")

    async def test_maybe_refresh_cookie_federated_expiring(
        self,
        session: AsyncSession,
        patched_enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        """Cookie with aid near expiry triggers server-side refresh."""
        enc = patched_enc
        user, access_row, _ = await self._make_user_and_tokens(session, enc, framework_config)
        access_row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.flush()

        from starlette.responses import Response
        from tests.unit.test_auth_service_coverage import _MockIdpClient

        from resourcey.auth.auth_dependencies import _maybe_refresh_cookie

        cookie = _mint_cookie_jwe(
            enc, user_id=user.id, access_id=access_row.id, access_expires_at=access_row.expires_at
        )

        # Patch AuthService to use mock IdP and no-op lock timeout.
        import resourcey.auth.auth_service as svc_mod

        original_init = svc_mod.AuthService.__init__

        def _patched_init(self: AuthService, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self._http = _MockIdpClient(
                token_response={
                    "access_token": "new",
                    "refresh_token": "new-r",
                    "expires_in": 3600,
                    "refresh_expires_in": 86400,
                }
            )
            self._owns_client = False

            async def _noop() -> None:
                pass

            self._set_lock_timeout = _noop  # type: ignore[method-assign]

        svc_mod.AuthService.__init__ = _patched_init  # type: ignore[assignment]
        try:
            resp = Response()
            await _maybe_refresh_cookie(cookie, resp, session)
            assert resp.headers.get("set-cookie")
        finally:
            svc_mod.AuthService.__init__ = original_init  # type: ignore[assignment]

    async def test_maybe_refresh_cookie_lock_timeout(
        self,
        session: AsyncSession,
        patched_enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        """When refresh_access_token raises RefreshLockTimeoutError, cookie is re-minted from fallback."""
        enc = patched_enc
        user, access_row, _ = await self._make_user_and_tokens(session, enc, framework_config)
        access_row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.flush()

        from starlette.responses import Response

        from resourcey.auth.auth_dependencies import _maybe_refresh_cookie

        cookie = _mint_cookie_jwe(
            enc, user_id=user.id, access_id=access_row.id, access_expires_at=access_row.expires_at
        )

        import resourcey.auth.auth_service as svc_mod

        original_refresh = svc_mod.AuthService.refresh_access_token

        async def _raise(self: AuthService, access_id: uuid.UUID) -> Any:
            raise RefreshLockTimeoutError("lock timeout")

        svc_mod.AuthService.refresh_access_token = _raise  # type: ignore[assignment]
        try:
            resp = Response()
            await _maybe_refresh_cookie(cookie, resp, session)
            assert resp.headers.get("set-cookie")
        finally:
            svc_mod.AuthService.refresh_access_token = original_refresh  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Full OAuth E2E: authorize -> dev/authorize -> callback -> token
# ---------------------------------------------------------------------------


class TestOAuthCallbackE2E:
    @pytest.fixture
    def app(
        self,
        framework_config: FrameworkConfig,
        patched_enc: EncryptionService,
    ) -> Any:
        enc = patched_enc

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(dev_router)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        app.state.resourcey_engine = engine
        app.state.resourcey_session_factory = async_sessionmaker(engine, expire_on_commit=False)

        import asyncio

        async def _create_tables() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(AuthBase.metadata.create_all)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_create_tables())
        loop.close()

        # Seed a user and an OAuth client.
        async def _seed() -> None:
            async with app.state.resourcey_session_factory() as sess:
                sess.add(
                    User(
                        email="e2e@example.com",
                        username="e2euser",
                        password=hash_password("e2epass"),
                        enabled=True,
                    )
                )
                auth_svc = AuthService(sess, encryption_service=enc, config=framework_config)
                await auth_svc.create_oauth_client(
                    client_id="client-1",
                    client_secret="secret-1",
                    redirect_uris=["http://localhost:8000/auth/callback"],
                )
                await sess.commit()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_seed())
        loop.close()

        yield app

        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine.dispose())
        loop.close()

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_idp_code_flow(self, app: Any) -> None:
        """/dev/authorize -> /dev/token without PKCE (direct dev IdP flow)."""
        import base64

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            # Call /auth/dev/authorize directly (no PKCE challenge).
            creds = base64.b64encode(b"e2euser:e2epass").decode()
            resp = client.get(
                "/auth/dev/authorize",
                params={
                    "client_id": "resourcey",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "response_type": "code",
                    "state": "xyz",
                },
                headers={"Authorization": f"Basic {creds}"},
                follow_redirects=False,
            )
            assert resp.status_code == 302, f"dev/authorize failed: {resp.text}"
            callback_url = resp.headers["location"]
            assert "code=" in callback_url

            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)
            code = params["code"][0]

            # Exchange the dev IdP code at /auth/dev/token.
            resp = client.post(
                "/auth/dev/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "client_id": "resourcey",
                    "client_secret": "changeme",
                },
            )
            assert resp.status_code == 200, f"dev/token failed: {resp.text}"
            dev_tokens = resp.json()
            assert "access_token" in dev_tokens
            assert "refresh_token" in dev_tokens

            # Refresh via /auth/dev/token.
            resp = client.post(
                "/auth/dev/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": dev_tokens["refresh_token"],
                    "client_id": "resourcey",
                    "client_secret": "changeme",
                },
            )
            assert resp.status_code == 200, f"dev refresh failed: {resp.text}"
            assert "access_token" in resp.json()

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_authorize_redirects_to_idp(self, app: Any) -> None:
        """/auth/authorize redirects to the dev IdP."""
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get(
                "/auth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "client-1",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "state": "client-state",
                    "scope": "openid email",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302
            idp_auth_url = resp.headers["location"]
            assert "/auth/dev/authorize" in idp_auth_url

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_callback_bad_state(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.get(
                "/auth/callback",
                params={"code": "fake", "state": "bad-state"},
                follow_redirects=False,
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_token_bad_code(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": "bad-code",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                },
            )
            assert resp.status_code == 400

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_token_bad_client(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": "x",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "client_id": "bad",
                    "client_secret": "bad",
                },
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_refresh_bad_token(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/refresh",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": "bad-token",
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                },
            )
            assert resp.status_code == 400

    def test_revoke_with_client(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/revoke",
                data={
                    "token": "some-token",
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                },
            )
            assert resp.status_code == 200

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_revoke_bad_client(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/revoke",
                data={"token": "x", "client_id": "bad", "client_secret": "bad"},
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_authorize_bad_credentials(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            import base64

            creds = base64.b64encode(b"e2euser:wrongpass").decode()
            resp = client.get(
                "/auth/dev/authorize",
                params={
                    "client_id": "resourcey",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "response_type": "code",
                },
                headers={"Authorization": f"Basic {creds}"},
                follow_redirects=False,
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_authorize_bad_redirect(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            import base64

            creds = base64.b64encode(b"e2euser:e2epass").decode()
            resp = client.get(
                "/auth/dev/authorize",
                params={
                    "client_id": "resourcey",
                    "redirect_uri": "http://evil.com/cb",
                    "response_type": "code",
                },
                headers={"Authorization": f"Basic {creds}"},
                follow_redirects=False,
            )
            assert resp.status_code == 400

    def test_dev_token_bad_client(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "x",
                    "redirect_uri": "http://localhost:8000/auth/callback",
                    "client_id": "bad",
                    "client_secret": "bad",
                },
            )
            assert resp.status_code == 401

    @pytest.mark.filterwarnings("ignore::Warning")
    def test_dev_token_missing_refresh_params(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            resp = client.post(
                "/auth/dev/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": "resourcey",
                    "client_secret": "changeme",
                },
            )
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Session: _get_or_create_factory fallback
# ---------------------------------------------------------------------------


class TestSessionFactoryFallback:
    async def test_get_or_create_factory_builds_from_config(
        self,
        framework_config: FrameworkConfig,
    ) -> None:
        set_config(framework_config)
        from starlette.requests import Request

        from resourcey.auth.session import _get_or_create_factory, dispose_app_engine

        app = FastAPI()
        # No factory on app.state -> should build one.
        req = Request(scope={"type": "http", "app": app, "headers": []})
        factory = _get_or_create_factory(req)
        assert factory is not None
        # Factory should be cached.
        assert getattr(app.state, "resourcey_session_factory", None) is factory
        await dispose_app_engine(app)


# ---------------------------------------------------------------------------
# auth_service: refresh_access_token success path
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    async def test_refresh_access_token_success(
        self,
        session: AsyncSession,
        patched_enc: EncryptionService,
        framework_config: FrameworkConfig,
    ) -> None:
        from tests.unit.test_auth_service_coverage import _make_user, _MockIdpClient

        enc = patched_enc
        auth_svc = AuthService(session, encryption_service=enc, config=framework_config)
        user = await _make_user(session)
        _refresh_row, access_row = await auth_svc.persist_idp_tokens(
            user.id,
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "refresh_expires_in": 86400,
            },
        )
        # Make access nearly expired.
        access_row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.flush()

        auth_svc._http = _MockIdpClient(
            token_response={
                "access_token": "new",
                "refresh_token": "new-r",
                "expires_in": 3600,
                "refresh_expires_in": 86400,
            }
        )
        auth_svc._owns_client = False

        async def _noop() -> None:
            pass

        auth_svc._set_lock_timeout = _noop  # type: ignore[method-assign]

        new_access, new_refresh = await auth_svc.refresh_access_token(access_row.id)
        # The refresh should return valid rows.
        assert new_access is not None
        assert new_refresh is not None
