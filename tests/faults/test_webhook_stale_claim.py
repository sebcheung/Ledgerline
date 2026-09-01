"""SPEC.md §10: a worker killed mid-delivery must not strand its claimed
rows in `delivering` forever -- `sweep_stale_claims` reclaims them back to
`pending`, and the event ID header lets a receiver dedupe the resulting
redelivery (README.md's at-least-once contract)."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.config import get_settings
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery, backdate_claim
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.timeout(30)]


async def _load(engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    WebhookDelivery.status,
                    WebhookDelivery.attempt_count,
                    WebhookDelivery.claimed_at,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


async def test_stale_claim_is_swept_back_to_pending_and_redelivered(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
) -> None:
    # Claim the row -- this is exactly what a worker does right before it
    # would deliver -- and then never call deliver()/`_record`. From the
    # database's point of view this is indistinguishable from the worker
    # process being SIGKILLed between the claim and the delivery: no
    # simulation of the crash is needed, only the absence of the step that
    # would normally follow it.
    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 1

    stuck = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert stuck["status"] == WebhookDeliveryStatus.DELIVERING
    assert stuck["claimed_at"] is not None
    assert stuck["attempt_count"] == 0

    # Not yet stale: the sweep must leave it alone.
    swept_too_soon = await dispatcher.sweep_stale_claims()
    assert swept_too_soon == 0

    settings = get_settings()
    await backdate_claim(
        concurrency_engine,
        wired_delivery.delivery_id,
        seconds=settings.webhook_stale_claim_seconds + 1,
    )

    swept = await dispatcher.sweep_stale_claims()
    assert swept == 1

    reclaimed = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert reclaimed["status"] == WebhookDeliveryStatus.PENDING
    assert reclaimed["claimed_at"] is None
    # The sweep does not charge the delivery an attempt -- the worker made
    # no observation of the receiver, so it must not shrink the retry
    # budget for a fault that was ours, not the receiver's.
    assert reclaimed["attempt_count"] == 0

    receiver_state.mode = FailureMode.OK
    redelivered = await dispatcher.claim_batch()
    assert len(redelivered) == 1
    await dispatcher.deliver(redelivered)

    final = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert final["status"] == WebhookDeliveryStatus.SUCCEEDED

    # This particular crash happened before the first HTTP call was ever
    # made, so the receiver only saw one request -- but the row was
    # claimed twice (once by the crashed attempt, once after the sweep).
    # A crash *after* the POST but before `_record` would have the receiver
    # see the same X-Ledgerline-Event-Id twice, which is exactly what makes
    # delivery at-least-once rather than exactly-once (README.md), and why
    # that header exists for receivers to dedupe on.
    assert len(receiver_state.received) == 1
    assert receiver_state.received[0].event_id == str(wired_delivery.event_id)
