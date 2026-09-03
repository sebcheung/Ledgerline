"""Shared fixtures for the SPEC.md §10 fault-injection suite."""

import asyncio
import random
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint
from ledger.webhooks.dispatcher import Dispatcher
from tests.mock_receiver.app import ReceiverState, create_receiver_app


@pytest_asyncio.fixture(autouse=True)
async def _clean_database(clean_database: None) -> None:
    """Mirrors `tests/integration/conftest.py`. `tests/faults/` is its own
    directory, so it needs its own autouse wrapper -- see
    `tests/conftest.py::clean_database` for why this isn't root-level."""


@pytest_asyncio.fixture
async def fault_client(
    migrated_database_url: str, concurrency_engine: AsyncEngine, clean_database: None
) -> AsyncGenerator[AsyncClient, None]:
    """Same shape as `app_client` (tests/conftest.py), but bound to
    `concurrency_engine`'s NullPool instead of the default-pooled
    `db_engine`. The concurrent-duplicate fault test sends 20 simultaneous
    requests that must land on 20 genuinely separate Postgres backends --
    the default pool (5 + 10 overflow) would silently serialize most of
    them and the test would pass without exercising SPEC.md §10's claim.

    Phase 7: seeds the same shared test API key as `app_client` and sends
    it on every request, so the fault suite keeps exercising real auth
    rather than being written against a bypassed dependency. Depends
    explicitly on `clean_database` so the seed runs after the truncate."""
    from tests.support.auth import TEST_API_KEY, seed_api_key

    await seed_api_key(concurrency_engine, raw_key=TEST_API_KEY)

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
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_API_KEY}"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def receiver_state() -> ReceiverState:
    return ReceiverState()


@pytest_asyncio.fixture
async def mock_receiver(receiver_state: ReceiverState) -> AsyncGenerator[str, None]:
    """A real uvicorn server on an ephemeral port -- not `httpx.
    ASGITransport`, which has no socket and cannot produce a
    `ReadTimeout`, `ConnectError`, or connection reset. All four SPEC.md
    §10 webhook faults (timeout, reset, 5xx/429, 400) therefore go through
    one real transport path, the same one `ledger.webhooks.dispatcher`
    uses in production."""
    app = create_receiver_app(receiver_state)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 -- uvicorn.Server exposes no event to await
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


@dataclass(frozen=True, slots=True)
class WiredDelivery:
    """A single pending delivery already wired to `mock_receiver`: an
    endpoint pointed at its `/hook` path (with `receiver_state.secret`
    already set to match), one outbox event, and one `pending`
    `webhook_deliveries` row -- so a fault test starts at `claim_batch()`
    rather than re-deriving `fan_out()` in every test."""

    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    delivery_id: uuid.UUID


@pytest_asyncio.fixture
async def wired_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    mock_receiver: str,
    receiver_state: ReceiverState,
) -> WiredDelivery:
    secret = "fault-test-secret"
    receiver_state.secret = secret
    async with session_factory() as session:
        endpoint_row = (
            await session.execute(
                insert(WebhookEndpoint)
                .values(url=f"{mock_receiver}/hook", secret=secret, active=True)
                .returning(WebhookEndpoint.id)
            )
        ).one()
        event_row = (
            await session.execute(
                insert(OutboxEvent)
                .values(event_type="transaction.posted", payload={"a": 1})
                .returning(OutboxEvent.id)
            )
        ).one()
        delivery_row = (
            await session.execute(
                insert(WebhookDelivery)
                .values(
                    event_id=event_row.id,
                    endpoint_id=endpoint_row.id,
                    status=WebhookDeliveryStatus.PENDING,
                    next_attempt_at=datetime.now(UTC),
                )
                .returning(WebhookDelivery.id)
            )
        ).one()
        await session.commit()
    return WiredDelivery(
        endpoint_id=endpoint_row.id, event_id=event_row.id, delivery_id=delivery_row.id
    )


@pytest_asyncio.fixture
async def dispatcher(concurrency_engine: AsyncEngine) -> AsyncGenerator[Dispatcher, None]:
    """A short read timeout (well under the receiver's default 0.2s
    TIMEOUT-mode delay) so the timeout fault test costs fractions of a
    second rather than the real 5s default."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=0.3, write=2.0, pool=2.0),
        follow_redirects=False,
    ) as client:
        yield Dispatcher(concurrency_engine, client, rng=random.Random(1))


async def backdate_next_attempt(engine: AsyncEngine, delivery_id: uuid.UUID, seconds: int) -> None:
    """Move a webhook delivery's `next_attempt_at` into the past from an
    independent connection, mirroring `backdate_lock` -- lets a retry test
    force a scheduled retry due without sleeping for the real delay."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE webhook_deliveries"
                " SET next_attempt_at = now() - make_interval(secs => :s)"
                " WHERE id = :id"
            ),
            {"s": seconds, "id": delivery_id},
        )


async def backdate_claim(engine: AsyncEngine, delivery_id: uuid.UUID, seconds: int) -> None:
    """Move a webhook delivery's `claimed_at` into the past, so a stale-
    claim test can force `sweep_stale_claims` to reclaim it without
    waiting out the real `webhook_stale_claim_seconds` window."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE webhook_deliveries"
                " SET claimed_at = now() - make_interval(secs => :s)"
                " WHERE id = :id"
            ),
            {"s": seconds, "id": delivery_id},
        )


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
