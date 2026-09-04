"""Pins Defect A: a graceful shutdown (SIGTERM) cancels the dispatcher's
task while `deliver()`'s `asyncio.gather` is in flight. Before the fix,
`asyncio.CancelledError` -- a `BaseException` in Python 3.8+ -- was not
caught by any of `_deliver_one`'s `except httpx.TimeoutException`/`except
httpx.TransportError`/`except Exception` clauses, so it *did* already
propagate correctly... except that path was never exercised or asserted
anywhere, so a change to `_deliver_one`'s broad `except Exception` (a
"broaden this catch" refactor, say) could silently start swallowing
cancellation instead of propagating it, with nothing to notice. This test
pins the behavior directly: cancellation must propagate out of
`deliver()`, and the row it interrupted must land exactly where
`claim_batch` left it (`delivering`), recoverable by the same
`sweep_stale_claims` path every other crash uses -- no special-casing for
"crashed via cancellation" vs. "crashed via SIGKILL".
"""

import asyncio
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

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]


async def _load(engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    WebhookDelivery.status,
                    WebhookDelivery.attempt_count,
                    WebhookDelivery.reclaim_count,
                    WebhookDelivery.claimed_at,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


async def test_cancellation_during_delivery_leaves_row_reclaimable(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
) -> None:
    # A long delay -- well past the short timeout `wait_for` below uses to
    # cancel the in-flight call, and well past the dispatcher fixture's own
    # 0.3s httpx read timeout would need to matter for this test: the
    # explicit cancellation must win the race, not httpx's own timeout.
    receiver_state.mode = FailureMode.TIMEOUT
    receiver_state.delay_seconds = 5.0

    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 1

    with pytest.raises((asyncio.CancelledError, TimeoutError)):
        await asyncio.wait_for(dispatcher.deliver(claimed), timeout=0.05)

    # The dispatcher never got to _record -- claim_batch's own write is all
    # that ever touched this row.
    stuck = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert stuck["status"] == WebhookDeliveryStatus.DELIVERING
    assert stuck["claimed_at"] is not None
    assert stuck["attempt_count"] == 0
    assert stuck["reclaim_count"] == 0

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
    assert reclaimed["attempt_count"] == 0
    assert reclaimed["reclaim_count"] == 1

    # Normal redelivery recovers the row exactly as any other reclaim would.
    receiver_state.mode = FailureMode.OK
    redelivered = await dispatcher.claim_batch()
    assert len(redelivered) == 1
    await dispatcher.deliver(redelivered)

    final = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert final["status"] == WebhookDeliveryStatus.SUCCEEDED
