"""Reconciliation run orchestration (SPEC.md §7).

Not named in SPEC.md §11's repo tree -- a deliberate addition (see
docs/DECISIONS.md Phase 4) so the route stays thin and a future
`worker/recon_scheduler.py` has one entry point to call instead of a
second code path.

Everything in `execute_run` happens inside the single DB transaction
`ledger.api.idempotent.IdempotentRequest.run` commits once at the end:
the advisory lock, the run row, every matched-line update, every finding
insert, and every resolver adjustment. A `ReconciliationRunFailed` raised
here rolls all of it back -- correct, because adjustments that would leave
invariant 7 broken must never commit.
"""

import logging
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.config import get_settings
from ledger.core.errors import ReconciliationRunFailed, ReconciliationRunInProgress
from ledger.core.invariants import verify_global_balance
from ledger.models.enums import ReconciliationRunStatus
from ledger.models.reconciliation import ReconciliationFinding, ReconciliationRun
from ledger.reconciliation import matcher, resolver

logger = logging.getLogger(__name__)

#: A literal, checked-in bigint -- never Python's hash(), which is
#: randomized per-process by PYTHONHASHSEED and would let two workers take
#: different locks for what must be one global lock. crc32 is deterministic
#: across processes and Python versions.
RECONCILIATION_RUN_LOCK_KEY: int = zlib.crc32(b"ledgerline:reconciliation_run")


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: uuid.UUID
    started_at: Any
    finished_at: Any
    window_start: Any
    window_end: Any
    cutoff_at: Any
    status: ReconciliationRunStatus
    findings_by_type: dict[str, Any]


async def _record_failed_run(
    engine: AsyncEngine,
    *,
    window_start: Any,
    window_end: Any,
    cutoff_at: Any,
    findings_by_type: dict[str, Any],
) -> None:
    """Audit trail for a run that failed post-run verification. Written on
    a **separate connection** from the request session, before the caller
    re-raises: the request's own transaction is about to roll back
    (correctly -- see module docstring), which would otherwise erase every
    trace that the run ever happened. This is the one place in Phase 4
    that writes outside the run's single commit."""
    async with engine.begin() as conn:
        await conn.execute(
            pg_insert(ReconciliationRun).values(
                window_start=window_start,
                window_end=window_end,
                cutoff_at=cutoff_at,
                status=ReconciliationRunStatus.FAILED,
                finished_at=text("now()"),
                findings_by_type=findings_by_type,
            )
        )


async def execute_run(session: AsyncSession, engine: AsyncEngine) -> RunResult:
    settings = get_settings()

    acquired = (
        await session.execute(
            select(text(f"pg_try_advisory_xact_lock({RECONCILIATION_RUN_LOCK_KEY})"))
        )
    ).scalar_one()
    if not acquired:
        raise ReconciliationRunInProgress("another reconciliation run is already in progress")

    # DB clock, not Python's -- consistent with docs/DECISIONS.md Phase 3's
    # "staleness is evaluated with the Postgres clock, not the app clock",
    # and with the fact that every timestamp this run compares against
    # (transactions.created_at, settlement_lines.ingested_at) was written
    # by the DB server.
    now_ts = (await session.execute(select(text("now()")))).scalar_one()
    window_end = now_ts
    window_start = window_end - timedelta(days=settings.recon_window_days)
    cutoff_at = window_end - timedelta(hours=settings.recon_cutoff_lag_hours)

    run_row = (
        await session.execute(
            pg_insert(ReconciliationRun)
            .values(
                started_at=now_ts,
                window_start=window_start,
                window_end=window_end,
                cutoff_at=cutoff_at,
                status=ReconciliationRunStatus.RUNNING,
            )
            .returning(ReconciliationRun.id)
        )
    ).one()
    run_id = run_row.id

    match_result = await matcher.match(
        session,
        window_start=window_start,
        window_end=window_end,
        cutoff_at=cutoff_at,
        fuzzy_days=settings.recon_fuzzy_days,
    )

    observed: dict[str, int] = {}
    for finding in match_result.findings:
        observed[finding.finding_type.value] = observed.get(finding.finding_type.value, 0) + 1

    created_rows: Sequence[Row[Any]] = []
    if match_result.findings:
        insert_stmt = (
            pg_insert(ReconciliationFinding)
            .values(
                [
                    {
                        "run_id": run_id,
                        "finding_type": f.finding_type,
                        "transaction_id": f.transaction_id,
                        "settlement_line_id": f.settlement_line_id,
                        "delta_amount": f.delta_amount,
                        "detail": f.detail,
                    }
                    for f in match_result.findings
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["finding_type", "transaction_id", "settlement_line_id"]
            )
            .returning(
                ReconciliationFinding.id,
                ReconciliationFinding.finding_type,
                ReconciliationFinding.transaction_id,
                ReconciliationFinding.settlement_line_id,
                ReconciliationFinding.delta_amount,
            )
        )
        created_rows = (await session.execute(insert_stmt)).all()

    created_counts = await resolver.resolve(session, created_rows)

    report = await verify_global_balance(session)
    if not report.ok:
        findings_by_type = {
            "observed": observed,
            "created": created_counts,
            "global_balance_failure": [
                {
                    "currency": c.currency,
                    "debit_total": c.debit_total,
                    "credit_total": c.credit_total,
                }
                for c in report.by_currency
                if not c.ok
            ],
        }
        await _record_failed_run(
            engine,
            window_start=window_start,
            window_end=window_end,
            cutoff_at=cutoff_at,
            findings_by_type=findings_by_type,
        )
        logger.error("reconciliation.run_failed", extra={"run_id": str(run_id)})
        raise ReconciliationRunFailed("post-run global balance verification failed", run_id=run_id)

    findings_by_type = {"observed": observed, "created": created_counts}
    finished_row = (
        await session.execute(
            update(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .values(
                status=ReconciliationRunStatus.COMPLETED,
                finished_at=text("now()"),
                findings_by_type=findings_by_type,
            )
            .returning(ReconciliationRun.finished_at)
        )
    ).one()

    logger.info(
        "reconciliation.run_completed",
        extra={"run_id": str(run_id), "observed": observed, "created": created_counts},
    )

    return RunResult(
        run_id=run_id,
        started_at=now_ts,
        finished_at=finished_row.finished_at,
        window_start=window_start,
        window_end=window_end,
        cutoff_at=cutoff_at,
        status=ReconciliationRunStatus.COMPLETED,
        findings_by_type=findings_by_type,
    )
