"""SPEC.md §9: `POST /v1/webhooks/deliveries/{id}/retry` is the manual DLQ
replay path -- a dead delivery, retried through the API, must be
redelivered by the dispatcher on its next cycle."""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.timeout(30)]


async def _load(engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(WebhookDelivery.status, WebhookDelivery.attempt_count).where(
                    WebhookDelivery.id == delivery_id
                )
            )
        ).one()
    return row._asdict()


async def test_manual_retry_of_a_dead_delivery_succeeds_on_the_next_cycle(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
    fault_client: AsyncClient,
) -> None:
    receiver_state.mode = FailureMode.STATUS
    receiver_state.status_code = 400

    claimed = await dispatcher.claim_batch()
    await dispatcher.deliver(claimed)
    dead = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert dead["status"] == WebhookDeliveryStatus.DEAD

    response = await fault_client.post(
        f"/v1/webhooks/deliveries/{wired_delivery.delivery_id}/retry"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["attempt_count"] == 0

    receiver_state.mode = FailureMode.OK
    redelivered = await dispatcher.claim_batch()
    assert len(redelivered) == 1
    await dispatcher.deliver(redelivered)

    final = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert final["status"] == WebhookDeliveryStatus.SUCCEEDED
    # attempt_count only ever counts failed attempts (it's a retry budget,
    # incremented in schedule_retry); a successful delivery doesn't touch
    # it. The reset-to-0 from the manual retry is what's pinned here, not
    # a continuation of the exhausted dead attempt count.
    assert final["attempt_count"] == 0
