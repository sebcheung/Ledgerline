"""Pins the `Dispatcher.run_forever` defect this slice fixes directly: with
no `try/except` around `await self.run_once()`, any exception escaping one
cycle -- most importantly a `sqlalchemy.exc.OperationalError` from a
Postgres backend that died mid-cycle -- used to kill the loop (and, in
`worker/webhook_worker.py`, the whole process) permanently.

The kill is genuine: `claim_batch`'s own connection is terminated
server-side from an independent connection (`admin_engine`) the first time
`run_forever` calls it, so the first cycle really does blow up with a real
`OperationalError`, not a simulated one. What we're pinning is that
`run_forever` survives that and a *later* cycle -- using an ordinary,
un-killed connection -- goes on to do real work.

This test is written to fail on the pre-fix code and pass on the fix; see
the slice report for the explicit before/after verification (temporarily
reverting the `try/except` and re-running this file).
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.webhooks.dispatcher import Dispatcher
from tests.chaos._pg_kill import backend_pid, kill_backend
from tests.faults.conftest import WiredDelivery
from tests.mock_receiver.app import FailureMode, ReceiverState

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]


async def test_run_forever_survives_a_killed_backend_and_keeps_working(
    wired_delivery: WiredDelivery,
    dispatcher: Dispatcher,
    receiver_state: ReceiverState,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver_state.mode = FailureMode.OK

    from ledger.config import get_settings

    # A short poll interval so a handful of run_forever cycles cost a
    # fraction of a second rather than the real default (1s).
    dispatcher._settings = get_settings().model_copy(update={"webhook_poll_interval_seconds": 0.05})

    call_count = {"n": 0}
    real_claim_batch = Dispatcher.claim_batch

    async def killing_claim_batch(self: Dispatcher) -> list[object]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Kill the exact backend claim_batch is about to use, then
            # force a real query on that now-dead connection -- a genuine
            # OperationalError, raised from inside the same code path
            # run_once() calls every cycle, not a synthetic stand-in.
            async with self._engine.begin() as conn:
                pid = await backend_pid(conn)
                assert await kill_backend(admin_engine, pid)
                await conn.execute(text("SELECT 1"))
            raise AssertionError("unreachable: the SELECT above must raise first")
        return await real_claim_batch(self)  # type: ignore[return-value]

    monkeypatch.setattr(Dispatcher, "claim_batch", killing_claim_batch)

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run_forever(stop))
    try:
        # Give run_forever several cycles: the first one dies on the killed
        # backend, later ones use ordinary fresh connections (NullPool) and
        # should succeed, delivering the wired row.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if call_count["n"] >= 2 and len(receiver_state.received) >= 1:
                break

        # The core assertion this test exists for: the worker task is still
        # running -- not finished, and definitely not raised -- after a
        # cycle that hit a real dead connection.
        assert not task.done(), f"run_forever died: {task.exception() if task.done() else None!r}"
        assert call_count["n"] >= 2, "run_forever never reached a second cycle"
        assert (
            len(receiver_state.received) >= 1
        ), "a later, healthy cycle never actually delivered the wired row"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
