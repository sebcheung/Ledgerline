"""`Dispatcher.fan_out()` (SPEC.md §8 "Fan-out"): one delivery row per
active endpoint, idempotent, bounded to the active endpoint set at the
moment an event is drained."""

import random
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """`fan_out`/`claim_batch` never make an HTTP call, but `Dispatcher`
    requires a client -- a real one, closed on teardown, rather than a
    module-level singleton that would outlive the test's event loop."""
    async with httpx.AsyncClient() as client:
        yield client


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": True}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _create_endpoint(app_client: AsyncClient, *, active: bool = True) -> str:
    response = await app_client.post(
        "/v1/webhooks/endpoints", json={"url": "http://127.0.0.1:9/hook", "active": active}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _dispatcher(engine: AsyncEngine, client: httpx.AsyncClient) -> Dispatcher:
    return Dispatcher(engine, client, rng=random.Random(1))


async def test_fanout_creates_one_row_per_active_endpoint(
    app_client: AsyncClient, db_engine: AsyncEngine, http_client: httpx.AsyncClient
) -> None:
    await _create_endpoint(app_client, active=True)
    await _create_endpoint(app_client, active=True)
    await _create_endpoint(app_client, active=False)

    cash = await _create_account(app_client, name="Cash")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 100, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 201

    dispatcher = _dispatcher(db_engine, http_client)
    fanned = await dispatcher.fan_out()
    assert fanned == 1

    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(WebhookDelivery.id))).scalars().all()
    assert len(rows) == 2

    # Re-running fan_out is a no-op: the event is already marked fanned.
    again = await dispatcher.fan_out()
    assert again == 0
    async with db_engine.connect() as conn:
        rows_after = (await conn.execute(select(WebhookDelivery.id))).scalars().all()
    assert len(rows_after) == 2


async def test_fanout_is_idempotent_even_if_replayed(
    app_client: AsyncClient, db_engine: AsyncEngine, http_client: httpx.AsyncClient
) -> None:
    """The `(event_id, endpoint_id)` unique constraint -- not
    `fanned_out_at` -- is the real fan-out idempotency guarantee: even if
    `fanned_out_at` is cleared and fan-out is forced to re-run over an
    already-fanned event, no duplicate delivery rows are created."""
    await _create_endpoint(app_client, active=True)
    cash = await _create_account(app_client, name="Cash")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 50, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 50, "currency": "USD"},
            ]
        },
    )

    dispatcher = _dispatcher(db_engine, http_client)
    await dispatcher.fan_out()

    async with db_engine.begin() as conn:
        await conn.execute(update(OutboxEvent).values(fanned_out_at=None))

    again = await dispatcher.fan_out()
    assert again == 1  # the event was re-claimed for fan-out...
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(WebhookDelivery.id))).scalars().all()
    assert len(rows) == 1  # ...but ON CONFLICT DO NOTHING absorbed the duplicate insert


async def test_fanout_with_zero_endpoints_marks_the_event_fanned(
    app_client: AsyncClient, db_engine: AsyncEngine, http_client: httpx.AsyncClient
) -> None:
    cash = await _create_account(app_client, name="Cash")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )

    dispatcher = _dispatcher(db_engine, http_client)
    fanned = await dispatcher.fan_out()
    assert fanned == 1

    async with db_engine.connect() as conn:
        deliveries = (await conn.execute(select(WebhookDelivery.id))).scalars().all()
        unfanned = (
            (await conn.execute(select(OutboxEvent.id).where(OutboxEvent.fanned_out_at.is_(None))))
            .scalars()
            .all()
        )
    assert deliveries == []
    assert unfanned == []  # the second poll has nothing left to do

    second = await dispatcher.fan_out()
    assert second == 0


async def test_endpoint_registered_after_fanout_does_not_get_the_event(
    app_client: AsyncClient, db_engine: AsyncEngine, http_client: httpx.AsyncClient
) -> None:
    cash = await _create_account(app_client, name="Cash")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    dispatcher = _dispatcher(db_engine, http_client)
    assert await dispatcher.fan_out() == 1

    await _create_endpoint(app_client, active=True)
    assert await dispatcher.fan_out() == 0
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(WebhookDelivery.id))).scalars().all()
    assert rows == []
