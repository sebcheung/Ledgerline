"""Integration-level proof that Phase 8 slice 1's `refresh_db_gauges` fix
(see `ledger.observability.metrics`'s module docstring) holds under a real
DB outage, not just the broken-session double
`tests/integration/test_metrics_endpoint.py::
test_metrics_endpoint_survives_a_db_outage_during_gauge_refresh` uses.

Technique: a dedicated `pool_size=1, max_overflow=0` engine (so there is
exactly one pooled connection to reason about, mirroring `admin_engine`'s
own reasoning for the same pool shape) is checked out once, its backend
PID captured and killed from an independent connection, then returned to
the pool *without* being closed on the Python side -- `pool_pre_ping` is
deliberately not set on this test engine, so the pool has no way to notice
the connection is already dead until something actually tries to use it.
`GET /metrics` is then pointed at this engine via the usual
`get_session` dependency override, so the query `refresh_db_gauges` issues
lands on the now-dead pooled connection and fails exactly the way a real
mid-scrape outage would.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import generate_latest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledger.observability.metrics import REGISTRY
from tests.chaos._pg_kill import backend_pid, kill_backend

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]


def _scalar(text: str, sample_name: str) -> float:
    from prometheus_client.parser import text_string_to_metric_families

    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == sample_name:
                return sample.value
    return 0.0


async def test_metrics_endpoint_survives_a_genuinely_killed_connection(
    migrated_database_url: str, admin_engine: AsyncEngine, clean_database: None
) -> None:
    # A single-connection pool: whatever backend serves the one connection
    # it ever hands out is entirely under this test's control.
    dead_engine = create_async_engine(migrated_database_url, pool_size=1, max_overflow=0)
    try:
        async with dead_engine.connect() as probe:
            pid = await backend_pid(probe)
            # Falls out of the `async with` normally (no exception), so the
            # underlying DBAPI connection is returned to the pool rather
            # than closed -- SQLAlchemy has no idea it's about to be killed.
        assert await kill_backend(admin_engine, pid), "target backend was already gone"

        from ledger.api.main import create_app
        from ledger.db.session import get_session

        session_factory = async_sessionmaker(dead_engine, expire_on_commit=False)

        async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    await session.rollback()

        app = create_app()
        app.dependency_overrides[get_session] = _override_get_session

        failures_before = _scalar(
            generate_latest(REGISTRY).decode(), "metrics_db_refresh_failures_total"
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/metrics")

        # The whole point: a genuinely dead pooled connection must not turn
        # /metrics into a 500. refresh_db_gauges swallows the error, bumps
        # metrics_db_refresh_failures_total, and the in-process counters
        # (which never touched the dead connection at all) are still there.
        assert response.status_code == 200
        text = response.text
        assert "transactions_posted_total" in text
        assert "entries_written_total" in text
        assert _scalar(text, "metrics_db_refresh_failures_total") == failures_before + 1
    finally:
        await dead_engine.dispose()
