"""Pins the "crashed after a 2xx, before `_record` commits" case: the
receiver has already accepted the delivery, but nothing in
`webhook_deliveries` reflects that yet. `sweep_stale_claims` must reclaim
the row (charging `reclaim_count`, not `attempt_count` -- the worker made
no *failed* observation, it just never got to record its success), and the
redelivery that follows must actually happen: the receiver sees the same
event a second time, which is exactly what the `X-Ledgerline-Event-Id`
header is for (README.md's at-least-once contract).

Simulated, not a real process kill: `SimulatedCrash` is a `BaseException`
(not `Exception`), which slips past `Dispatcher.deliver`'s `await
self._record(outcomes)` the way a SIGKILL would -- see
`tests/faults/test_idempotency_crash.py`'s docstring for the same
technique.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery, backdate_claim, ledger_row_counts
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
                    WebhookDelivery.claimed_at,
                ).where(WebhookDelivery.id == delivery_id)
            )
        ).one()
    return row._asdict()


async def test_crash_after_success_before_record_reclaims_and_redelivers(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver_state.mode = FailureMode.OK
    before_counts = await ledger_row_counts(db_session)

    real_record = Dispatcher._record
    call_count = {"n": 0}

    async def flaky_record(self: Dispatcher, outcomes: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:  # crash on the very first _record -- after
            # the POST already succeeded (the receiver already answered
            # 200), but before anything is written to webhook_deliveries.
            raise SimulatedCrash("simulated crash after 2xx, before _record commits")
        await real_record(self, outcomes)  # type: ignore[arg-type]

    monkeypatch.setattr(Dispatcher, "_record", flaky_record)

    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 1

    with pytest.raises(SimulatedCrash):
        await dispatcher.deliver(claimed)

    # The receiver really did answer 200 -- one real HTTP round trip
    # happened -- but the crash means webhook_deliveries never heard about
    # it: the row is still exactly where claim_batch left it.
    assert len(receiver_state.received) == 1
    first_event_id = receiver_state.received[0].event_id

    stuck = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert stuck["status"] == WebhookDeliveryStatus.DELIVERING
    assert stuck["claimed_at"] is not None
    assert stuck["attempt_count"] == 0
    assert stuck["reclaim_count"] == 0

    from ledger.config import get_settings

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
    # The reclaim is charged to reclaim_count, not attempt_count -- the
    # worker never observed a failure from the receiver, so its retry
    # budget (attempt_count) must not shrink.
    assert reclaimed["attempt_count"] == 0
    assert reclaimed["reclaim_count"] == 1

    # Redeliver for real this time -- _record's third call (call_count is
    # now 2, so the *next* call is the first that goes through the real
    # implementation).
    redelivered = await dispatcher.claim_batch()
    assert len(redelivered) == 1
    await dispatcher.deliver(redelivered)

    final = await _load(concurrency_engine, wired_delivery.delivery_id)
    assert final["status"] == WebhookDeliveryStatus.SUCCEEDED

    # The receiver saw the same event twice -- this is the crux of
    # at-least-once delivery: the crash happened *after* the POST, so the
    # redelivery is a genuine duplicate, not merely a second attempt at
    # something the receiver never saw.
    assert len(receiver_state.received) == 2
    assert receiver_state.received[1].event_id == first_event_id
    assert receiver_state.received[1].event_id == str(wired_delivery.event_id)

    # Zero ledger-table side effects from any of this -- webhook delivery
    # (crashed or not) never touches transactions/entries/outbox_events.
    assert await ledger_row_counts(db_session) == before_counts
