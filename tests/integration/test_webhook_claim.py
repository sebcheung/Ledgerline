"""`Dispatcher.claim_batch()`'s `FOR UPDATE SKIP LOCKED` claim: two
dispatchers racing over the same pending rows must claim disjoint sets
without blocking on each other (SPEC.md §8)."""

import asyncio
import random
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ledger.config import get_settings
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient() as client:
        yield client


async def _create_endpoint(app_client: AsyncClient) -> str:
    response = await app_client.post(
        "/v1/webhooks/endpoints", json={"url": "http://127.0.0.1:9/hook"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _insert_pending_deliveries(
    session_factory: async_sessionmaker[Any], *, endpoint_id: uuid.UUID, count: int
) -> None:
    async with session_factory() as session:
        for _ in range(count):
            event_row = (
                await session.execute(
                    insert(OutboxEvent)
                    .values(event_type="transaction.posted", payload={})
                    .returning(OutboxEvent.id)
                )
            ).one()
            await session.execute(
                insert(WebhookDelivery).values(
                    event_id=event_row.id,
                    endpoint_id=endpoint_id,
                    status=WebhookDeliveryStatus.PENDING,
                    next_attempt_at=datetime.now(UTC),
                )
            )
        await session.commit()


async def test_two_dispatchers_claim_disjoint_sets_without_blocking(
    app_client: AsyncClient,
    concurrency_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    http_client: httpx.AsyncClient,
) -> None:
    """`concurrency_engine`'s NullPool guarantees each dispatcher gets a
    genuinely separate Postgres backend -- the property this test depends
    on, the same reason `test_concurrency.py` uses it."""
    endpoint_id = uuid.UUID(await _create_endpoint(app_client))
    await _insert_pending_deliveries(session_factory, endpoint_id=endpoint_id, count=20)

    settings = get_settings().model_copy(update={"webhook_batch_size": 10})
    dispatcher_a = Dispatcher(
        concurrency_engine, http_client, settings=settings, rng=random.Random(1)
    )
    dispatcher_b = Dispatcher(
        concurrency_engine, http_client, settings=settings, rng=random.Random(2)
    )

    claimed_a, claimed_b = await asyncio.gather(
        dispatcher_a.claim_batch(), dispatcher_b.claim_batch()
    )

    ids_a = {row.id for row in claimed_a}
    ids_b = {row.id for row in claimed_b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a) + len(ids_b) == 20
