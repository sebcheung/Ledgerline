"""Integration tests for rate limiting on `/v1` (SPEC.md §9 Phase 7).

Builds its own app with a tiny configured limit and a fake clock, rather
than reusing `app_client` -- 100 req/s burst 200 (the real default) would
make a 429 test either slow (200+ requests) or reliant on real sleeps.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.api.ratelimit import RateLimiter
from tests.support.auth import seed_api_key

pytestmark = pytest.mark.integration

_ANY_ACCOUNT_PATH = "/v1/accounts/00000000-0000-0000-0000-000000000000"


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


async def _client_with_limit(
    db_engine: AsyncEngine, *, rate: float, burst: float, clock: FakeClock, raw_key: str
) -> AsyncClient:
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    await seed_api_key(db_engine, raw_key=raw_key)

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    app.state.rate_limiter = RateLimiter(rate_per_second=rate, burst=burst, clock=clock)

    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw_key}"},
    )


async def test_third_request_over_a_burst_of_two_is_429(
    db_engine: AsyncEngine, clean_database: None
) -> None:
    clock = FakeClock()
    async with await _client_with_limit(
        db_engine, rate=1.0, burst=2.0, clock=clock, raw_key="lk_rl_burst2"
    ) as client:
        first = await client.get(_ANY_ACCOUNT_PATH)
        second = await client.get(_ANY_ACCOUNT_PATH)
        third = await client.get(_ANY_ACCOUNT_PATH)

    assert first.status_code == 404  # allowed through to the route
    assert second.status_code == 404
    assert third.status_code == 429
    assert third.headers["content-type"] == "application/problem+json"
    assert third.json()["type"] == "/errors/rate-limited"
    assert int(third.headers["retry-after"]) >= 1


async def test_a_second_key_has_its_own_bucket(
    db_engine: AsyncEngine, clean_database: None
) -> None:
    clock = FakeClock()
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    await seed_api_key(db_engine, raw_key="lk_rl_shared_a")
    await seed_api_key(db_engine, raw_key="lk_rl_shared_b")

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    app.state.rate_limiter = RateLimiter(rate_per_second=1.0, burst=1.0, clock=clock)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        exhaust = await client.get(
            _ANY_ACCOUNT_PATH, headers={"Authorization": "Bearer lk_rl_shared_a"}
        )
        denied = await client.get(
            _ANY_ACCOUNT_PATH, headers={"Authorization": "Bearer lk_rl_shared_a"}
        )
        other_key = await client.get(
            _ANY_ACCOUNT_PATH, headers={"Authorization": "Bearer lk_rl_shared_b"}
        )

    assert exhaust.status_code == 404
    assert denied.status_code == 429
    assert other_key.status_code == 404


async def test_advancing_the_clock_re_admits_a_request(
    db_engine: AsyncEngine, clean_database: None
) -> None:
    clock = FakeClock()
    async with await _client_with_limit(
        db_engine, rate=1.0, burst=1.0, clock=clock, raw_key="lk_rl_refill"
    ) as client:
        first = await client.get(_ANY_ACCOUNT_PATH)
        denied = await client.get(_ANY_ACCOUNT_PATH)
        clock.now = 1.0
        third = await client.get(_ANY_ACCOUNT_PATH)

    assert first.status_code == 404
    assert denied.status_code == 429
    assert third.status_code == 404


async def test_rate_limit_disabled_setting_never_429s(
    monkeypatch: pytest.MonkeyPatch, db_engine: AsyncEngine, clean_database: None
) -> None:
    from ledger.config import get_settings

    disabled_settings = get_settings().model_copy(update={"rate_limit_enabled": False})
    monkeypatch.setattr("ledger.api.ratelimit.get_settings", lambda: disabled_settings)

    clock = FakeClock()
    async with await _client_with_limit(
        db_engine, rate=1.0, burst=1.0, clock=clock, raw_key="lk_rl_disabled"
    ) as client:
        responses = [await client.get(_ANY_ACCOUNT_PATH) for _ in range(5)]

    assert all(r.status_code == 404 for r in responses)
