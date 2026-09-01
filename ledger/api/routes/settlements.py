from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ledger.api.deps import SessionDep
from ledger.models.settlements import SettlementLine
from ledger.reconciliation.ingest import ingest_batch
from ledger.schemas.pagination import Page, decode_cursor, encode_cursor
from ledger.schemas.settlements import (
    SettlementIngestRequest,
    SettlementIngestResponse,
    SettlementLineRead,
    SettlementListQuery,
)

router = APIRouter()


@router.post("/settlements/ingest", response_model=SettlementIngestResponse, status_code=201)
async def ingest(payload: SettlementIngestRequest, session: SessionDep) -> SettlementIngestResponse:
    result = await ingest_batch(session, payload.lines)
    await session.commit()
    return SettlementIngestResponse(
        batch_id=result.batch_id, ingested=result.ingested, deduplicated=result.deduplicated
    )


@router.get("/settlements", response_model=Page[SettlementLineRead])
async def list_settlements(
    session: SessionDep, query: Annotated[SettlementListQuery, Query()]
) -> Page[SettlementLineRead]:
    stmt = select(
        SettlementLine.id,
        SettlementLine.external_ref,
        SettlementLine.amount,
        SettlementLine.currency,
        SettlementLine.value_date,
        SettlementLine.raw,
        SettlementLine.batch_id,
        SettlementLine.ingested_at,
        SettlementLine.matched_transaction_id,
    )
    if query.batch_id is not None:
        stmt = stmt.where(SettlementLine.batch_id == query.batch_id)
    if query.matched is not None:
        stmt = stmt.where(
            SettlementLine.matched_transaction_id.is_not(None)
            if query.matched
            else SettlementLine.matched_transaction_id.is_(None)
        )

    if query.cursor is not None:
        c = decode_cursor(query.cursor)
        stmt = stmt.where(
            (SettlementLine.ingested_at < c.created_at)
            | ((SettlementLine.ingested_at == c.created_at) & (SettlementLine.id < c.id))
        )

    stmt = stmt.order_by(SettlementLine.ingested_at.desc(), SettlementLine.id.desc()).limit(
        query.limit + 1
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]

    items = [
        SettlementLineRead(
            id=r.id,
            external_ref=r.external_ref,
            amount=r.amount,
            currency=r.currency,
            value_date=r.value_date,
            raw=r.raw,
            batch_id=r.batch_id,
            ingested_at=r.ingested_at,
            matched_transaction_id=r.matched_transaction_id,
        )
        for r in rows
    ]
    next_cursor = encode_cursor(rows[-1].ingested_at, rows[-1].id) if has_more and rows else None
    return Page[SettlementLineRead](items=items, next_cursor=next_cursor, has_more=has_more)
