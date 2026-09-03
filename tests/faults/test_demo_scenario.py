"""Fault-suite coverage for `dashboard.demo.run_demo` (SPEC.md §12 Phase 6).

Lives in `tests/faults/`, not `tests/integration/`, because it needs the
same real-socket failure this directory already sets up for the webhook
fault suite (`mock_receiver`, a real uvicorn server): a `dead` delivery
produced by an `UPDATE` would prove nothing about the demo's own webhook
registration, only about the test's ability to write SQL.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from dashboard.demo import run_demo
from ledger.core.invariants import verify_global_balance
from ledger.models.enums import ReconciliationFindingType, ReconciliationResolution
from ledger.models.reconciliation import ReconciliationFinding, ReconciliationRun

pytestmark = [pytest.mark.integration, pytest.mark.fault]


async def test_run_demo_produces_findings_and_real_dead_and_pending_deliveries(
    db_session: AsyncSession, concurrency_engine: AsyncEngine, mock_receiver: str
) -> None:
    """The dead delivery must come from the mock receiver actually
    answering (even a signature-mismatch 401 is a real HTTP round trip
    against a real socket) -- not from a test writing `status='dead'`
    directly, which would make the dashboard's DLQ panel prove nothing."""
    result = await run_demo(
        db_session, concurrency_engine, rng_seed=1, dead_endpoint_url=f"{mock_receiver}/hook"
    )
    assert result.ran
    assert result.endpoints_registered == 2
    assert result.deliveries_dead >= 1
    assert result.deliveries_pending >= 1

    observed = result.findings_by_type["observed"] if result.findings_by_type else {}
    assert any(
        observed.get(k, 0) > 0
        for k in (
            "missing_settlement",
            "duplicate_settlement",
            "unexpected_settlement",
            "amount_mismatch",
        )
    )


async def test_run_demo_produces_both_auto_resolved_and_unresolved_amount_mismatch(
    db_session: AsyncSession, concurrency_engine: AsyncEngine, mock_receiver: str
) -> None:
    """`_DRIFT.perturb_max_minor` (800) deliberately straddles the default
    auto-resolve threshold (500), so a run should yield both outcomes --
    this is what makes "recovery is visible" concrete on the dashboard.
    `rng_seed=0` is pinned rather than arbitrary: with only ~20 candidate
    transactions per run, not every seed's perturbation draws happen to
    land on both sides of the threshold, and this one is confirmed (by an
    offline search) to do so."""
    result = await run_demo(
        db_session, concurrency_engine, rng_seed=0, dead_endpoint_url=f"{mock_receiver}/hook"
    )
    assert result.ran

    stmt = (
        select(ReconciliationFinding.resolution, func.count().label("n"))
        .where(
            ReconciliationFinding.run_id == result.run_id,
            ReconciliationFinding.finding_type == ReconciliationFindingType.AMOUNT_MISMATCH,
        )
        .group_by(ReconciliationFinding.resolution)
    )
    rows = {r.resolution: r.n for r in (await db_session.execute(stmt))}
    assert rows.get(ReconciliationResolution.AUTO_RESOLVED, 0) >= 1
    assert rows.get(ReconciliationResolution.UNRESOLVED, 0) >= 1


async def test_run_demo_leaves_global_balance_intact(
    db_session: AsyncSession, concurrency_engine: AsyncEngine, mock_receiver: str
) -> None:
    result = await run_demo(
        db_session, concurrency_engine, rng_seed=3, dead_endpoint_url=f"{mock_receiver}/hook"
    )
    assert result.ran
    report = await verify_global_balance(db_session)
    assert report.ok


async def test_run_demo_is_rerunnable_and_appends_rather_than_replaces(
    db_session: AsyncSession, concurrency_engine: AsyncEngine, mock_receiver: str
) -> None:
    """Deliberately not idempotent: a second click appends a fresh scenario
    instead of being a no-op, so the run count must go 1 -> 2."""
    first = await run_demo(
        db_session, concurrency_engine, rng_seed=5, dead_endpoint_url=f"{mock_receiver}/hook"
    )
    assert first.ran
    second = await run_demo(
        db_session, concurrency_engine, rng_seed=5, dead_endpoint_url=f"{mock_receiver}/hook"
    )
    assert second.ran
    assert second.run_id != first.run_id
    # The second call finds the demo accounts already exist.
    assert second.accounts_created == 0

    run_count = (
        await db_session.execute(select(func.count()).select_from(ReconciliationRun))
    ).scalar_one()
    assert run_count == 2

    report = await verify_global_balance(db_session)
    assert report.ok


async def test_run_demo_locked_out_by_a_concurrent_run_reports_ran_false(
    session_factory: async_sessionmaker[AsyncSession],
    concurrency_engine: AsyncEngine,
    mock_receiver: str,
) -> None:
    """A held session-level advisory lock on `DEMO_LOCK_KEY` -- Postgres
    advisory locks are cluster-wide regardless of session- vs
    transaction-level form, so this correctly blocks `run_demo`'s
    `pg_try_advisory_xact_lock` on a second, independent connection."""
    from dashboard.demo import DEMO_LOCK_KEY

    async with session_factory() as holder:
        acquired = (
            await holder.execute(select(func.pg_try_advisory_lock(DEMO_LOCK_KEY)))
        ).scalar_one()
        assert acquired

        async with session_factory() as blocked:
            result = await run_demo(
                blocked, concurrency_engine, rng_seed=9, dead_endpoint_url=f"{mock_receiver}/hook"
            )
        assert result.ran is False

        await holder.execute(select(func.pg_advisory_unlock(DEMO_LOCK_KEY)))
