"""SPEC.md §10: a webhook receiver 400 is a client error that will not fix
itself -- immediately `dead`, no retries. Also: `MAX_ATTEMPTS` exhaustion
from repeated 5xx eventually goes `dead` too, with every scheduled delay
bounded by the configured cap."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.config import get_settings
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery, backdate_next_attempt
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.timeout(30)]


async def _load(engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    WebhookDelivery.status,
                    WebhookDelivery.attempt_count,
                    WebhookDelivery.last_response_code,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


async def test_400_is_dead_after_exactly_one_attempt_with_no_retries(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
) -> None:
    receiver_state.mode = FailureMode.STATUS
    receiver_state.status_code = 400

    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 1
    await dispatcher.deliver(claimed)

    row = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert row["status"] == WebhookDeliveryStatus.DEAD
    assert row["attempt_count"] == 1
    assert row["last_response_code"] == 400

    # A dead row is never claimed again -- it is not `pending`.
    claimed_again = await dispatcher.claim_batch()
    assert claimed_again == []


async def test_max_attempts_exhaustion_goes_dead_with_bounded_delays(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
) -> None:
    receiver_state.mode = FailureMode.STATUS
    receiver_state.status_code = 500
    settings = get_settings()

    for expected_attempt in range(1, settings.webhook_max_attempts + 1):
        claimed = await dispatcher.claim_batch()
        assert len(claimed) == 1, f"expected a claimable row before attempt {expected_attempt}"
        await dispatcher.deliver(claimed)

        row = await _load(concurrency_engine, wired_delivery.delivery_id)
        assert row["attempt_count"] == expected_attempt
        if expected_attempt < settings.webhook_max_attempts:
            assert row["status"] == WebhookDeliveryStatus.PENDING
            # Force the next retry due immediately -- every delay is
            # bounded by webhook_max_delay_seconds regardless, so there is
            # nothing to lose by not waiting for it.
            await backdate_next_attempt(
                concurrency_engine, wired_delivery.delivery_id, seconds=3600
            )
        else:
            assert row["status"] == WebhookDeliveryStatus.DEAD

    assert row["attempt_count"] == settings.webhook_max_attempts
    assert await dispatcher.claim_batch() == []
