"""Pins `_record`'s all-or-nothing transaction: `Dispatcher._record` writes
every outcome in a batch inside one `async with self._engine.begin()`
block, so a crash while processing the *second* delivery in a two-row
batch must roll back the *first* delivery's already-issued UPDATE too --
not leave it half-recorded. Both rows come back from the crash exactly as
`claim_batch` left them (`delivering`), get reclaimed by the same sweep,
and are redelivered successfully.

Simulated, not a real process kill: `SimulatedCrash` is a `BaseException`
(not `Exception`), matching `tests/faults/test_idempotency_crash.py`'s
documented technique. Here it's injected via the dispatcher module's
logger rather than `_record` itself, so the crash lands *inside* the
transaction (after the first row's UPDATE was already sent, before the
second row's log line that follows its own UPDATE) instead of before any
work happens.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import ledger.webhooks.dispatcher as dispatcher_module
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery
from ledger.webhooks.dispatcher import Dispatcher
from tests.faults.conftest import WiredDelivery, backdate_claim
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]


class SimulatedCrash(BaseException):
    pass


async def _statuses(engine: AsyncEngine, delivery_ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    WebhookDelivery.id, WebhookDelivery.status, WebhookDelivery.claimed_at
                ).where(WebhookDelivery.id.in_(delivery_ids))
            )
        ).all()
    return {r.id: r for r in rows}


async def _add_second_delivery(
    session_factory: async_sessionmaker[AsyncSession], endpoint_id: uuid.UUID
) -> uuid.UUID:
    """A second `pending` delivery against the same endpoint as
    `wired_delivery` -- so `claim_batch` claims both in one batch without
    needing a second mock receiver."""
    async with session_factory() as session:
        event_row = (
            await session.execute(
                insert(OutboxEvent)
                .values(event_type="transaction.posted", payload={"a": 2})
                .returning(OutboxEvent.id)
            )
        ).one()
        delivery_row = (
            await session.execute(
                insert(WebhookDelivery)
                .values(
                    event_id=event_row.id,
                    endpoint_id=endpoint_id,
                    status=WebhookDeliveryStatus.PENDING,
                    next_attempt_at=datetime.now(UTC),
                )
                .returning(WebhookDelivery.id)
            )
        ).one()
        await session.commit()
    return uuid.UUID(str(delivery_row.id))


async def test_crash_mid_batch_record_rolls_back_the_whole_batch(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    concurrency_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver_state.mode = FailureMode.OK
    second_delivery_id = await _add_second_delivery(session_factory, wired_delivery.endpoint_id)
    all_ids = [wired_delivery.delivery_id, second_delivery_id]

    claimed = await dispatcher.claim_batch()
    assert len(claimed) == 2
    assert {c.id for c in claimed} == set(all_ids)

    real_info = dispatcher_module.logger.info
    call_count = {"n": 0}

    def flaky_info(msg: object, *args: object, extra: dict[str, object] | None = None) -> None:
        call_count["n"] += 1
        # The first "webhook.delivered" log line closes out the first
        # outcome's UPDATE; the second call -- for the second outcome --
        # is where the worker dies, with the first row's UPDATE already
        # issued (but not yet committed) inside the same transaction.
        if call_count["n"] == 2:
            raise SimulatedCrash("simulated crash mid-batch, inside _record's transaction")
        real_info(msg, *args, extra=extra)

    monkeypatch.setattr(dispatcher_module.logger, "info", flaky_info)

    with pytest.raises(SimulatedCrash):
        await dispatcher.deliver(claimed)

    monkeypatch.undo()

    # Both real HTTP round trips happened -- the receiver saw both events --
    # but the crash means neither row's outcome was committed.
    assert len(receiver_state.received) == 2

    statuses = await _statuses(concurrency_engine, all_ids)
    for delivery_id in all_ids:
        row = statuses[delivery_id]
        assert row.status == WebhookDeliveryStatus.DELIVERING, (
            f"{delivery_id} should still be delivering -- _record's transaction "
            "must be all-or-nothing"
        )
        assert row.claimed_at is not None

    from ledger.config import get_settings

    settings = get_settings()
    for delivery_id in all_ids:
        await backdate_claim(
            concurrency_engine, delivery_id, seconds=settings.webhook_stale_claim_seconds + 1
        )

    swept = await dispatcher.sweep_stale_claims()
    assert swept == 2

    reclaimed = await _statuses(concurrency_engine, all_ids)
    for delivery_id in all_ids:
        assert reclaimed[delivery_id].status == WebhookDeliveryStatus.PENDING

    redelivered = await dispatcher.claim_batch()
    assert len(redelivered) == 2
    await dispatcher.deliver(redelivered)

    final = await _statuses(concurrency_engine, all_ids)
    for delivery_id in all_ids:
        assert final[delivery_id].status == WebhookDeliveryStatus.SUCCEEDED

    # Both events redelivered -- 2 from the crashed attempt + 2 from the
    # real redelivery.
    assert len(receiver_state.received) == 4
