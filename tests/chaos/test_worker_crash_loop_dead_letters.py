"""Pins Defect B: a worker that reliably crashes mid-POST (every single
attempt, forever) must eventually be dead-lettered instead of redelivered
without end. `sweep_stale_claims` deliberately never charges
`attempt_count` for a reclaim -- the worker made no observation of the
receiver -- so without a separate `reclaim_count` budget this row would
cycle `delivering` -> (crash) -> `pending` -> `delivering` forever. This
test crashes every `_record` call, sweeps past `webhook_max_reclaims`
times, and asserts the row lands on `dead` with a `last_error` that says
why, and stays there -- a further sweep must not touch it.

Simulated, not a real process kill: `SimulatedCrash` is a `BaseException`
(not `Exception`), matching `tests/faults/test_idempotency_crash.py`'s
documented technique.
"""

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


class SimulatedCrash(BaseException):
    pass


async def _load(engine: AsyncEngine, delivery_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    WebhookDelivery.status,
                    WebhookDelivery.attempt_count,
                    WebhookDelivery.reclaim_count,
                    WebhookDelivery.last_error,
                    WebhookDelivery.claimed_at,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


async def test_a_permanently_crashing_worker_eventually_dead_letters(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver_state.mode = FailureMode.OK
    settings = get_settings()

    async def always_boom(self: Dispatcher, outcomes: object) -> None:
        raise SimulatedCrash("simulated crash before _record, every single time")

    monkeypatch.setattr(Dispatcher, "_record", always_boom)

    # Reclaim `webhook_max_reclaims` times: each cycle is claim -> crash ->
    # backdate past the stale window -> sweep. Every one of these except the
    # last reclaims back to `pending`.
    for cycle in range(settings.webhook_max_reclaims):
        claimed = await dispatcher.claim_batch()
        assert len(claimed) == 1, f"cycle {cycle}: row should still be claimable"

        with pytest.raises(SimulatedCrash):
            await dispatcher.deliver(claimed)

        await backdate_claim(
            concurrency_engine,
            wired_delivery.delivery_id,
            seconds=settings.webhook_stale_claim_seconds + 1,
        )
        swept = await dispatcher.sweep_stale_claims()
        assert swept == 1, f"cycle {cycle}: sweep should reclaim the stuck row"

        row = await _load(concurrency_engine, wired_delivery.delivery_id)
        assert row["reclaim_count"] == cycle + 1
        assert row["attempt_count"] == 0  # never charged, even at exhaustion

        if cycle + 1 >= settings.webhook_max_reclaims:
            assert row["status"] == WebhookDeliveryStatus.DEAD
            assert row["last_error"] is not None
            assert "reclaim" in row["last_error"].lower()
        else:
            assert row["status"] == WebhookDeliveryStatus.PENDING

    dead = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert dead["status"] == WebhookDeliveryStatus.DEAD

    # The row is no longer `delivering`, so it is not a sweep candidate at
    # all -- claim_batch (PENDING only) and sweep_stale_claims (DELIVERING
    # only) both leave it alone from here on.
    claimed_again = await dispatcher.claim_batch()
    assert claimed_again == []

    swept_again = await dispatcher.sweep_stale_claims()
    assert swept_again == 0

    still_dead = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert still_dead["status"] == WebhookDeliveryStatus.DEAD
    assert still_dead["reclaim_count"] == settings.webhook_max_reclaims
