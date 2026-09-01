import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query, Response
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult

from ledger.api.deps import SessionDep
from ledger.api.idempotent import IdempotentResult, ReconciliationIdempotencyDep
from ledger.core.errors import (
    FindingAlreadyResolved,
    ReconciliationFindingNotFound,
    ReconciliationRunNotFound,
)
from ledger.db.engine import engine
from ledger.models.enums import ReconciliationResolution
from ledger.models.reconciliation import ReconciliationFinding, ReconciliationRun
from ledger.reconciliation import runner
from ledger.reconciliation.resolver import resolve_manual_adjustment
from ledger.schemas.pagination import Page, decode_cursor, encode_cursor
from ledger.schemas.reconciliation import (
    FindingResolveAction,
    FindingResolveRequest,
    ReconciliationFindingListQuery,
    ReconciliationFindingRead,
    ReconciliationRunRead,
)

router = APIRouter()


@router.post("/reconciliation/runs", response_model=ReconciliationRunRead, status_code=201)
async def create_run(
    session: SessionDep, response: Response, idem: ReconciliationIdempotencyDep
) -> ReconciliationRunRead:
    async def execute() -> IdempotentResult[ReconciliationRunRead]:
        result = await runner.execute_run(session, engine)
        body = ReconciliationRunRead(
            id=result.run_id,
            started_at=result.started_at,
            finished_at=result.finished_at,
            window_start=result.window_start,
            window_end=result.window_end,
            cutoff_at=result.cutoff_at,
            status=result.status.value,
            findings_by_type=result.findings_by_type,
        )
        return IdempotentResult(
            status=201, body=body, headers={"Location": f"/v1/reconciliation/runs/{result.run_id}"}
        )

    result = await idem.run(execute)
    response.headers.update(result.headers)
    return result.body


@router.get("/reconciliation/runs/{run_id}", response_model=ReconciliationRunRead)
async def get_run(run_id: uuid.UUID, session: SessionDep) -> ReconciliationRunRead:
    row = (
        await session.execute(
            select(
                ReconciliationRun.id,
                ReconciliationRun.started_at,
                ReconciliationRun.finished_at,
                ReconciliationRun.window_start,
                ReconciliationRun.window_end,
                ReconciliationRun.cutoff_at,
                ReconciliationRun.status,
                ReconciliationRun.findings_by_type,
            ).where(ReconciliationRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise ReconciliationRunNotFound(f"reconciliation run {run_id} not found", run_id=run_id)
    return ReconciliationRunRead(
        id=row.id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        window_start=row.window_start,
        window_end=row.window_end,
        cutoff_at=row.cutoff_at,
        status=row.status.value,
        findings_by_type=row.findings_by_type,
    )


@router.get(
    "/reconciliation/runs/{run_id}/findings", response_model=Page[ReconciliationFindingRead]
)
async def list_findings(
    run_id: uuid.UUID,
    session: SessionDep,
    query: Annotated[ReconciliationFindingListQuery, Query()],
) -> Page[ReconciliationFindingRead]:
    stmt = select(
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
    ).where(ReconciliationFinding.run_id == run_id)
    if query.resolution is not None:
        stmt = stmt.where(ReconciliationFinding.resolution == query.resolution)
    if query.cursor is not None:
        c = decode_cursor(query.cursor)
        stmt = stmt.where(
            (ReconciliationFinding.created_at < c.created_at)
            | (
                (ReconciliationFinding.created_at == c.created_at)
                & (ReconciliationFinding.id < c.id)
            )
        )
    stmt = stmt.order_by(
        ReconciliationFinding.created_at.desc(), ReconciliationFinding.id.desc()
    ).limit(query.limit + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]
    items = [ReconciliationFindingRead.model_validate(r) for r in rows]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return Page[ReconciliationFindingRead](items=items, next_cursor=next_cursor, has_more=has_more)


@router.post(
    "/reconciliation/findings/{finding_id}/resolve", response_model=ReconciliationFindingRead
)
async def resolve_finding(
    finding_id: uuid.UUID, payload: FindingResolveRequest, session: SessionDep
) -> ReconciliationFindingRead:
    """Not on SPEC.md §6's idempotent-endpoint list, and it posts real
    money -- guarded instead by `SELECT ... FOR UPDATE` plus a
    compare-and-swap `UPDATE ... WHERE resolution = 'unresolved'`, the same
    layered-guards shape docs/DECISIONS.md uses for double-reversal."""
    row = (
        await session.execute(
            select(
                ReconciliationFinding.id,
                ReconciliationFinding.finding_type,
                ReconciliationFinding.transaction_id,
                ReconciliationFinding.settlement_line_id,
                ReconciliationFinding.delta_amount,
                ReconciliationFinding.detail,
                ReconciliationFinding.resolution,
            )
            .where(ReconciliationFinding.id == finding_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise ReconciliationFindingNotFound(
            f"reconciliation finding {finding_id} not found", finding_id=finding_id
        )
    if row.resolution != ReconciliationResolution.UNRESOLVED:
        raise FindingAlreadyResolved(
            f"finding {finding_id} is already {row.resolution.value}", finding_id=finding_id
        )

    resolving_transaction_id: uuid.UUID | None = None
    if payload.action is FindingResolveAction.POST_ADJUSTMENT:
        resolving_transaction_id = await resolve_manual_adjustment(session, row)

    detail: dict[str, Any] | None = None
    if payload.note is not None:
        detail = {**(row.detail or {}), "note": payload.note}

    values: dict[str, Any] = {
        "resolution": ReconciliationResolution.MANUALLY_RESOLVED,
        "resolving_transaction_id": resolving_transaction_id,
        "resolved_at": text("now()"),
    }
    if detail is not None:
        values["detail"] = detail

    cas_result = cast(
        CursorResult[Any],
        await session.execute(
            update(ReconciliationFinding)
            .where(
                ReconciliationFinding.id == finding_id,
                ReconciliationFinding.resolution == ReconciliationResolution.UNRESOLVED,
            )
            .values(**values)
        ),
    )
    if cas_result.rowcount != 1:
        raise FindingAlreadyResolved(
            f"finding {finding_id} was resolved concurrently", finding_id=finding_id
        )

    await session.commit()

    final_row = (
        await session.execute(
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
            ).where(ReconciliationFinding.id == finding_id)
        )
    ).one()
    return ReconciliationFindingRead.model_validate(final_row)
