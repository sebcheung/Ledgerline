"""Recent-transaction read model for the dashboard's transactions panel."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.entries import Entry
from ledger.models.enums import EntryDirection, TransactionSource, TransactionStatus
from ledger.models.transactions import Transaction


@dataclass(frozen=True, slots=True)
class RecentTransactionRow:
    id: uuid.UUID
    external_ref: str | None
    description: str | None
    status: TransactionStatus
    source: TransactionSource
    reversal_of: uuid.UUID | None
    created_at: datetime
    currency: str
    amount: int
    entry_count: int


async def load_recent_transactions(session: AsyncSession, limit: int) -> list[RecentTransactionRow]:
    """The debit-sum aggregation idiom proven in
    `ledger.reconciliation.feed`'s loader and `ledger.reconciliation.matcher`
    -- amount is `sum(debits)`, equal to `sum(credits)` by invariant 1. The
    `id` tiebreak is load-bearing: Postgres `now()` is transaction-start
    time, so every entry written by one `post_transaction` call shares an
    identical `created_at` (see the pagination cursor's own reasoning,
    docs/DECISIONS.md)."""
    amount = cast(
        func.sum(case((Entry.direction == EntryDirection.DEBIT, Entry.amount), else_=0)),
        BigInteger,
    ).label("amount")

    stmt = (
        select(
            Transaction.id,
            Transaction.external_ref,
            Transaction.description,
            Transaction.status,
            Transaction.source,
            Transaction.reversal_of,
            Transaction.created_at,
            func.min(Entry.currency).label("currency"),
            amount,
            func.count(Entry.id).label("entry_count"),
        )
        .join(Entry, Entry.transaction_id == Transaction.id)
        .group_by(
            Transaction.id,
            Transaction.external_ref,
            Transaction.description,
            Transaction.status,
            Transaction.source,
            Transaction.reversal_of,
            Transaction.created_at,
        )
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RecentTransactionRow(
            id=r.id,
            external_ref=r.external_ref,
            description=r.description,
            status=r.status,
            source=r.source,
            reversal_of=r.reversal_of,
            created_at=r.created_at,
            currency=r.currency,
            amount=r.amount,
            entry_count=r.entry_count,
        )
        for r in rows
    ]
