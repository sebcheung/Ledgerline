import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from ledger.api.deps import IdempotencyKeyDep, SessionDep
from ledger.core.errors import TransactionNotFound
from ledger.core.posting import EntryRequest, post_transaction, reverse_transaction
from ledger.models.entries import Entry
from ledger.models.enums import TransactionSource
from ledger.models.transactions import Transaction
from ledger.schemas.accounts import EntryRead
from ledger.schemas.pagination import Page, decode_cursor, encode_cursor
from ledger.schemas.transactions import (
    TransactionCreate,
    TransactionListQuery,
    TransactionRead,
    TransactionSummary,
)

router = APIRouter()

_TRANSACTION_COLUMNS = (
    Transaction.id,
    Transaction.idempotency_key,
    Transaction.external_ref,
    Transaction.description,
    Transaction.status,
    Transaction.reversal_of,
    Transaction.source,
    Transaction.created_at,
)


async def _load_transaction_read(session: SessionDep, transaction_id: uuid.UUID) -> TransactionRead:
    txn_row = (
        await session.execute(select(*_TRANSACTION_COLUMNS).where(Transaction.id == transaction_id))
    ).one_or_none()
    if txn_row is None:
        raise TransactionNotFound(
            f"transaction {transaction_id} not found", transaction_id=transaction_id
        )

    entry_rows = (
        await session.execute(
            select(
                Entry.id,
                Entry.transaction_id,
                Entry.account_id,
                Entry.direction,
                Entry.amount,
                Entry.currency,
                Entry.created_at,
            )
            .where(Entry.transaction_id == transaction_id)
            .order_by(Entry.created_at, Entry.id)
        )
    ).all()

    entries = [
        EntryRead(
            id=r.id,
            transaction_id=r.transaction_id,
            account_id=r.account_id,
            direction=r.direction,
            amount=r.amount,
            currency=r.currency,
            created_at=r.created_at,
        )
        for r in entry_rows
    ]
    currency = entries[0].currency if entries else ""
    return TransactionRead(
        id=txn_row.id,
        idempotency_key=txn_row.idempotency_key,
        external_ref=txn_row.external_ref,
        description=txn_row.description,
        status=txn_row.status,
        reversal_of=txn_row.reversal_of,
        source=txn_row.source,
        created_at=txn_row.created_at,
        currency=currency,
        entries=entries,
    )


@router.post("/transactions", response_model=TransactionRead, status_code=201)
async def create_transaction(
    payload: TransactionCreate,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
) -> TransactionRead:
    posted = await post_transaction(
        session,
        [EntryRequest(e.account_id, e.direction, e.amount, e.currency) for e in payload.entries],
        idempotency_key=idempotency_key,
        external_ref=payload.external_ref,
        description=payload.description,
        source=TransactionSource.API,
    )
    await session.commit()
    response.headers["Location"] = f"/v1/transactions/{posted.id}"
    return TransactionRead.model_validate(posted)


@router.get("/transactions/{transaction_id}", response_model=TransactionRead)
async def get_transaction(transaction_id: uuid.UUID, session: SessionDep) -> TransactionRead:
    return await _load_transaction_read(session, transaction_id)


@router.get("/transactions", response_model=Page[TransactionSummary])
async def list_transactions(
    session: SessionDep, query: Annotated[TransactionListQuery, Query()]
) -> Page[TransactionSummary]:
    stmt = select(
        Transaction.id,
        Transaction.external_ref,
        Transaction.description,
        Transaction.status,
        Transaction.reversal_of,
        Transaction.source,
        Transaction.created_at,
    )
    if query.external_ref is not None:
        stmt = stmt.where(Transaction.external_ref == query.external_ref)
    if query.status is not None:
        stmt = stmt.where(Transaction.status == query.status)
    if query.created_after is not None:
        stmt = stmt.where(Transaction.created_at >= query.created_after)
    if query.created_before is not None:
        stmt = stmt.where(Transaction.created_at < query.created_before)

    if query.cursor is not None:
        c = decode_cursor(query.cursor)
        stmt = stmt.where(
            (Transaction.created_at < c.created_at)
            | ((Transaction.created_at == c.created_at) & (Transaction.id < c.id))
        )

    stmt = stmt.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(
        query.limit + 1
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]

    items = [
        TransactionSummary(
            id=r.id,
            external_ref=r.external_ref,
            description=r.description,
            status=r.status,
            reversal_of=r.reversal_of,
            source=r.source,
            created_at=r.created_at,
        )
        for r in rows
    ]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return Page[TransactionSummary](items=items, next_cursor=next_cursor, has_more=has_more)


@router.post(
    "/transactions/{transaction_id}/reverse", response_model=TransactionRead, status_code=201
)
async def reverse(
    transaction_id: uuid.UUID,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKeyDep,
) -> TransactionRead:
    posted = await reverse_transaction(session, transaction_id, idempotency_key=idempotency_key)
    await session.commit()
    response.headers["Location"] = f"/v1/transactions/{posted.id}"
    return TransactionRead.model_validate(posted)
