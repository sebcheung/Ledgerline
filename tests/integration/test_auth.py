"""Integration tests for Bearer API key auth on `/v1` (SPEC.md §9 Phase 7).

`unauthenticated_client` is the same app as `app_client` (tests/conftest.py)
with no `Authorization` header, for asserting the failure shapes; `app_client`
itself already carries a valid seeded key and is what every non-auth test in
the suite uses, proving those routes stay reachable under auth.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.models.api_keys import ApiKey
from tests.support.auth import TEST_API_KEY, seed_api_key

pytestmark = pytest.mark.integration

_ANY_ACCOUNT_PATH = "/v1/accounts/00000000-0000-0000-0000-000000000000"


async def test_no_header_is_401(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.get(_ANY_ACCOUNT_PATH)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["type"] == "/errors/unauthenticated"


async def test_wrong_scheme_is_401(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.get(
        _ANY_ACCOUNT_PATH, headers={"Authorization": f"Basic {TEST_API_KEY}"}
    )
    assert response.status_code == 401


async def test_malformed_header_is_401(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.get(
        _ANY_ACCOUNT_PATH, headers={"Authorization": "garbage"}
    )
    assert response.status_code == 401


async def test_unknown_key_is_401(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.get(
        _ANY_ACCOUNT_PATH,
        headers={"Authorization": "Bearer lk_this_key_was_never_minted"},
    )
    assert response.status_code == 401
    assert response.json()["type"] == "/errors/unauthenticated"


async def test_inactive_key_is_401(
    unauthenticated_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    raw_key = "lk_inactive_test_key"
    await seed_api_key(db_engine, raw_key=raw_key, active=False)

    response = await unauthenticated_client.get(
        _ANY_ACCOUNT_PATH, headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 401


async def test_valid_key_is_200(app_client: AsyncClient) -> None:
    response = await app_client.get(_ANY_ACCOUNT_PATH)
    # 404 (account not found), not 401 -- proves auth passed and the
    # request reached the route.
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics", "/dashboard"])
async def test_unauthenticated_surfaces_reachable_without_a_key(
    unauthenticated_client: AsyncClient, path: str
) -> None:
    response = await unauthenticated_client.get(path)
    assert response.status_code != 401


async def test_revoking_a_key_denies_the_next_request_when_cache_is_disabled(
    monkeypatch: pytest.MonkeyPatch, db_engine: AsyncEngine
) -> None:
    """With `api_key_cache_ttl_seconds=0`, revocation must take effect on
    the very next request -- proving the cache, not the auth check itself,
    is what owns the up-to-`TTL` propagation delay documented in
    `ledger.api.auth.ApiKeyCache`."""
    from ledger.api.main import create_app
    from ledger.config import get_settings
    from ledger.db.session import get_session

    disabled_cache_settings = get_settings().model_copy(update={"api_key_cache_ttl_seconds": 0.0})
    monkeypatch.setattr("ledger.api.main.get_settings", lambda: disabled_cache_settings)

    raw_key = "lk_ttl_zero_test_key"
    key_id = await seed_api_key(db_engine, raw_key=raw_key)

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw_key}"},
    ) as client:
        first = await client.get(_ANY_ACCOUNT_PATH)
        assert first.status_code == 404

        async with session_factory() as session:
            await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(active=False))
            await session.commit()

        second = await client.get(_ANY_ACCOUNT_PATH)
        assert second.status_code == 401
