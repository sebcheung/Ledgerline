"""Shared fixtures for the SPEC.md §10 fault-injection suite."""

from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture(autouse=True)
async def _clean_database(clean_database: None) -> None:
    """Mirrors `tests/integration/conftest.py`. `tests/faults/` is its own
    directory, so it needs its own autouse wrapper -- see
    `tests/conftest.py::clean_database` for why this isn't root-level."""


@pytest_asyncio.fixture
async def fault_client(
    migrated_database_url: str, concurrency_engine: AsyncEngine
) -> AsyncGenerator[AsyncClient, None]:
    """Same shape as `app_client` (tests/conftest.py), but bound to
    `concurrency_engine`'s NullPool instead of the default-pooled
    `db_engine`. The concurrent-duplicate fault test sends 20 simultaneous
    requests that must land on 20 genuinely separate Postgres backends --
    the default pool (5 + 10 overflow) would silently serialize most of
    them and the test would pass without exercising SPEC.md §10's claim."""
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def backdate_lock(engine: AsyncEngine, key: str, seconds: int) -> None:
    """Move an idempotency key's `locked_at` into the past from an
    independent connection, so a test can force staleness without ever
    sleeping for the real TTL."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys"
                " SET locked_at = now() - make_interval(secs => :s)"
                " WHERE key = :k"
            ),
            {"s": seconds, "k": key},
        )


async def ledger_row_counts(session: AsyncSession) -> tuple[int, int, int]:
    """(transactions, entries, outbox_events) row counts -- the invariant
    every fault test checks: exactly one ledger effect no matter how many
    times a request was retried."""
    txns = (await session.execute(text("SELECT COUNT(*) FROM transactions"))).scalar_one()
    entries = (await session.execute(text("SELECT COUNT(*) FROM entries"))).scalar_one()
    outbox = (await session.execute(text("SELECT COUNT(*) FROM outbox_events"))).scalar_one()
    return int(txns), int(entries), int(outbox)
