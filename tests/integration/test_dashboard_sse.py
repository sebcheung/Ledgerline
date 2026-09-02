"""Integration tests for `GET /dashboard/sse` (SPEC.md §12 Phase 6).

Every test bounds `StreamConfig.max_events` via `app.dependency_overrides`
(the same mechanism `tests/conftest.py`'s `app_client` already uses for
`get_session`) so the generator drains to completion instead of the test
abandoning a live one -- see `dashboard/sse.py` and docs/DECISIONS.md for
why that matters under `filterwarnings = ["error"]`.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _make_sse_client(
    db_engine: AsyncEngine, *, max_events: int, keepalive_seconds: float = 15.0
) -> AsyncClient:
    from dashboard.sse import StreamConfig
    from dashboard.views import get_dashboard_session_factory, get_stream_config
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_dashboard_session_factory] = lambda: session_factory
    app.dependency_overrides[get_stream_config] = lambda: StreamConfig(
        interval_seconds=0.0, keepalive_seconds=keepalive_seconds, max_events=max_events
    )
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def sse_client_one_tick(db_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    async with await _make_sse_client(db_engine, max_events=1) as client:
        yield client


@pytest.mark.timeout(10)
async def test_sse_stream_emits_one_of_each_event_on_the_first_tick(
    sse_client_one_tick: AsyncClient,
) -> None:
    async with sse_client_one_tick.stream("GET", "/dashboard/sse") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert body.startswith(": connected\n\n")
    for panel in ("balances", "transactions", "reconciliation", "webhooks"):
        assert body.count(f"event: {panel}") == 1


@pytest.mark.timeout(10)
async def test_sse_second_tick_with_no_change_emits_only_a_heartbeat(
    db_engine: AsyncEngine,
) -> None:
    async with (
        await _make_sse_client(db_engine, max_events=2, keepalive_seconds=0.0) as client,
        client.stream("GET", "/dashboard/sse") as response,
    ):
        body = "".join([chunk async for chunk in response.aiter_text()])

    # Every panel appears exactly once (tick 1's initial snapshot); tick 2
    # sees nothing changed and, with keepalive_seconds=0, must emit a
    # heartbeat instead of re-sending unchanged fragments.
    for panel in ("balances", "transactions", "reconciliation", "webhooks"):
        assert body.count(f"event: {panel}") == 1
    assert ": keep-alive" in body


@pytest.mark.timeout(10)
async def test_sse_reflects_a_transaction_committed_from_another_session(
    db_engine: AsyncEngine,
) -> None:
    """Proves `event_stream` opens a *fresh* session every tick rather than
    caching a snapshot from generator construction -- the single likeliest
    SSE bug, and invisible to a test that never writes between ticks."""
    client = await _make_sse_client(db_engine, max_events=2, keepalive_seconds=0.0)
    async with client:
        create = await client.post(
            "/v1/accounts",
            json={
                "name": "SSE Test Account",
                "type": "asset",
                "currency": "USD",
                "allow_negative": True,
            },
        )
        assert create.status_code == 201, create.text

        async with client.stream("GET", "/dashboard/sse") as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    assert "SSE Test Account" in body


@pytest.mark.timeout(10)
async def test_sse_early_disconnect_does_not_hang(db_engine: AsyncEngine) -> None:
    """The one test that deliberately abandons a stream mid-read, in
    isolation -- if ASGITransport's cancellation semantics ever change, this
    one named test fails instead of the whole suite going flaky. `max_events`
    is a generous, but still finite, safety net: it bounds worst-case
    runtime even if `request.is_disconnected()` never fires under
    `ASGITransport` (it has no real socket to observe), so this test cannot
    hang the suite regardless of that detail of the transport."""
    async with (
        await _make_sse_client(db_engine, max_events=50) as client,
        client.stream("GET", "/dashboard/sse") as response,
    ):
        async for _chunk in response.aiter_bytes():
            break
        await response.aclose()


@pytest.mark.timeout(10)
async def test_sse_holds_no_pool_connection_between_ticks(db_engine: AsyncEngine) -> None:
    baseline = db_engine.pool.checkedout()  # type: ignore[attr-defined]
    async with (
        await _make_sse_client(db_engine, max_events=3, keepalive_seconds=0.0) as client,
        client.stream("GET", "/dashboard/sse") as response,
    ):
        async for _chunk in response.aiter_text():
            assert db_engine.pool.checkedout() <= baseline  # type: ignore[attr-defined]
