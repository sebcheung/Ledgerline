"""SPEC.md §10: webhook receiver timeout / 500 / connection reset / 429 ->
retry with backoff; success once the receiver recovers."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery, backdate_next_attempt
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.timeout(30)]


async def _load(concurrency_engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with concurrency_engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    WebhookDelivery.status,
                    WebhookDelivery.attempt_count,
                    WebhookDelivery.next_attempt_at,
                    WebhookDelivery.last_error,
                    WebhookDelivery.last_response_code,
                    WebhookDelivery.claimed_at,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


@pytest.mark.parametrize(
    ("mode", "status_code"),
    [
        (FailureMode.STATUS, 500),
        (FailureMode.STATUS, 429),
        (FailureMode.TIMEOUT, None),
        (FailureMode.RESET, None),
    ],
)
async def test_transient_fault_retries_then_succeeds_on_recovery(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
    mode: FailureMode,
    status_code: int | None,
) -> None:
    receiver_state.mode = mode
    if status_code is not None:
        receiver_state.status_code = status_code
    if mode is FailureMode.TIMEOUT:
        receiver_state.delay_seconds = 1.0  # exceeds the dispatcher's 0.3s read timeout

    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 1
    await dispatcher.deliver(claimed)

    row = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert row["status"] == WebhookDeliveryStatus.PENDING
    assert row["attempt_count"] == 1
    assert row["claimed_at"] is None
    if mode is FailureMode.STATUS:
        assert row["last_response_code"] == status_code
    else:
        assert row["last_response_code"] is None
    assert row["last_error"]

    # Every attempt -- including this failed one -- must carry a valid
    # signature, proven from the receiver's own record, not just from the
    # dispatcher's code path.
    assert len(receiver_state.received) == 1
    assert receiver_state.received[0].signature_valid is True

    # Force the scheduled retry due without sleeping for the real backoff.
    await backdate_next_attempt(concurrency_engine, wired_delivery.delivery_id, seconds=3600)
    receiver_state.mode = FailureMode.OK

    claimed_again = await dispatcher.claim_batch()
    assert len(claimed_again) == 1
    await dispatcher.deliver(claimed_again)

    final_row = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert final_row["status"] == WebhookDeliveryStatus.SUCCEEDED
    assert len(receiver_state.received) == 2
    assert all(r.signature_valid for r in receiver_state.received)
