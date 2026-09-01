"""`Dispatcher.run_once()`/`run_forever()`: the composed cycle (sweep ->
fan-out -> claim -> deliver) and the poll loop that drives it in
production, exercised directly rather than only through their component
steps."""

import asyncio
import random

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.webhooks.dispatcher import Dispatcher

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": True}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_run_once_composes_sweep_fanout_claim_and_deliver(
    app_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    response = await app_client.post(
        "/v1/webhooks/endpoints", json={"url": "http://127.0.0.1:9/hook"}
    )
    assert response.status_code == 201

    cash = await _create_account(app_client, name="Cash")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 5, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 5, "currency": "USD"},
            ]
        },
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=0.5, read=0.5, write=0.5, pool=0.5)
    ) as client:
        dispatcher = Dispatcher(db_engine, client, rng=random.Random(1))
        cycle = await dispatcher.run_once()

    # The endpoint is unreachable (nothing listens on :9/hook), so the
    # single fanned-out delivery is claimed and then scheduled for retry --
    # this exercises every step of the cycle in one call.
    assert cycle.swept == 0
    assert cycle.fanned_out == 1
    assert cycle.claimed == 1
    assert cycle.retried == 1
    assert cycle.succeeded == 0
    assert cycle.dead == 0


async def test_run_forever_stops_when_the_event_is_set(db_engine: AsyncEngine) -> None:
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(db_engine, client, rng=random.Random(1))
        stop = asyncio.Event()

        async def _stop_after_first_cycle() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        stopper = asyncio.create_task(_stop_after_first_cycle())
        await asyncio.wait_for(dispatcher.run_forever(stop), timeout=5)
        await stopper

    assert stop.is_set()
