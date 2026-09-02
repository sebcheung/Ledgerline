"""Reconciliation run/finding read model for the dashboard's reconciliation
panel."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.enums import (
    ReconciliationFindingType,
    ReconciliationResolution,
    ReconciliationRunStatus,
)
from ledger.models.reconciliation import ReconciliationFinding, ReconciliationRun


@dataclass(frozen=True, slots=True)
class ReconciliationRunRow:
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    window_start: datetime
    window_end: datetime
    cutoff_at: datetime
    status: ReconciliationRunStatus
    findings_by_type: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ReconciliationFindingRow:
    id: uuid.UUID
    run_id: uuid.UUID
    finding_type: ReconciliationFindingType
    transaction_id: uuid.UUID | None
    settlement_line_id: uuid.UUID | None
    delta_amount: int | None
    detail: dict[str, Any] | None
    resolution: ReconciliationResolution
    resolving_transaction_id: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FindingTypeCount:
    finding_type: ReconciliationFindingType
    resolution: ReconciliationResolution
    count: int


async def load_run_history(session: AsyncSession, limit: int) -> list[ReconciliationRunRow]:
    """`reconciliation_runs` has no index on `started_at` -- one row per run,
    so a seq scan + sort is free at this scale; see docs/DECISIONS.md Phase 6
    for the deferred index and its trigger condition."""
    stmt = (
        select(
            ReconciliationRun.id,
            ReconciliationRun.started_at,
            ReconciliationRun.finished_at,
            ReconciliationRun.window_start,
            ReconciliationRun.window_end,
            ReconciliationRun.cutoff_at,
            ReconciliationRun.status,
            ReconciliationRun.findings_by_type,
        )
        .order_by(ReconciliationRun.started_at.desc(), ReconciliationRun.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ReconciliationRunRow(
            id=r.id,
            started_at=r.started_at,
            finished_at=r.finished_at,
            window_start=r.window_start,
            window_end=r.window_end,
            cutoff_at=r.cutoff_at,
            status=r.status,
            findings_by_type=r.findings_by_type,
        )
        for r in rows
    ]


async def load_findings_for_run(
    session: AsyncSession, run_id: uuid.UUID, limit: int
) -> list[ReconciliationFindingRow]:
    """Served by `ix_reconciliation_findings_run_id`, the same index
    `GET /v1/reconciliation/runs/{id}/findings` uses."""
    stmt = (
        select(
            ReconciliationFinding.id,
            ReconciliationFinding.run_id,
            ReconciliationFinding.finding_type,
            ReconciliationFinding.transaction_id,
            ReconciliationFinding.settlement_line_id,
            ReconciliationFinding.delta_amount,
            ReconciliationFinding.detail,
            ReconciliationFinding.resolution,
            ReconciliationFinding.resolving_transaction_id,
            ReconciliationFinding.resolved_at,
            ReconciliationFinding.created_at,
        )
        .where(ReconciliationFinding.run_id == run_id)
        .order_by(ReconciliationFinding.created_at.desc(), ReconciliationFinding.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ReconciliationFindingRow(
            id=r.id,
            run_id=r.run_id,
            finding_type=r.finding_type,
            transaction_id=r.transaction_id,
            settlement_line_id=r.settlement_line_id,
            delta_amount=r.delta_amount,
            detail=r.detail,
            resolution=r.resolution,
            resolving_transaction_id=r.resolving_transaction_id,
            resolved_at=r.resolved_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def load_findings_summary(session: AsyncSession) -> list[FindingTypeCount]:
    """Cross-run open-drift summary -- what an operator actually watches,
    as opposed to one run's findings alone."""
    stmt = select(
        ReconciliationFinding.finding_type,
        ReconciliationFinding.resolution,
        func.count().label("n"),
    ).group_by(ReconciliationFinding.finding_type, ReconciliationFinding.resolution)
    rows = (await session.execute(stmt)).all()
    return [
        FindingTypeCount(finding_type=r.finding_type, resolution=r.resolution, count=r.n)
        for r in rows
    ]
