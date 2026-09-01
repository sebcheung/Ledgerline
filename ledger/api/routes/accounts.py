import uuid

from fastapi import APIRouter, Response
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.deps import SessionDep
from ledger.core.errors import (
    AccountNotFound,
    ClearingAccountExists,
    InvalidCursor,
    SuspenseAccountExists,
)
from ledger.db.errors import constraint_name_of
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.entries import Entry
from ledger.schemas.accounts import AccountCreate, AccountRead, EntryRead
from ledger.schemas.pagination import Page, decode_cursor, encode_cursor

router = APIRouter()

_CONSTRAINT_SUSPENSE_PER_CURRENCY = "uq_accounts_suspense_per_currency"
_CONSTRAINT_CLEARING_PER_CURRENCY = "uq_accounts_clearing_per_currency"


async def _find_suspense_account(session: AsyncSession, currency: str) -> uuid.UUID | None:
    return (
        await session.execute(
            select(Account.id).where(Account.currency == currency, Account.is_suspense.is_(True))
        )
    ).scalar_one_or_none()


async def _find_clearing_account(session: AsyncSession, currency: str) -> uuid.UUID | None:
    return (
        await session.execute(
            select(Account.id).where(Account.currency == currency, Account.is_clearing.is_(True))
        )
    ).scalar_one_or_none()


@router.post("/accounts", response_model=AccountRead, status_code=201)
async def create_account(
    payload: AccountCreate, session: SessionDep, response: Response
) -> AccountRead:
    try:
        async with session.begin_nested():
            row = (
                await session.execute(
                    insert(Account)
                    .values(
                        name=payload.name,
                        type=payload.type,
                        currency=payload.currency,
                        allow_negative=payload.allow_negative,
                        is_suspense=payload.is_suspense,
                        is_clearing=payload.is_clearing,
                    )
                    .returning(
                        Account.id,
                        Account.name,
                        Account.type,
                        Account.currency,
                        Account.allow_negative,
                        Account.is_suspense,
                        Account.is_clearing,
                        Account.created_at,
                    )
                )
            ).one()
            # Created in the same DB transaction as the account -- this is
            # what makes posting.py's "missing balance row" self-heal path
            # unreachable in ordinary operation.
            await session.execute(
                insert(AccountBalance).values(
                    account_id=row.id, currency=payload.currency, balance=0, entry_count=0
                )
            )
    except IntegrityError as exc:
        constraint = constraint_name_of(exc)
        if constraint == _CONSTRAINT_SUSPENSE_PER_CURRENCY:
            existing = await _find_suspense_account(session, payload.currency)
            raise SuspenseAccountExists(
                f"a suspense account for {payload.currency} already exists",
                currency=payload.currency,
                existing_account_id=existing,
            ) from exc
        if constraint == _CONSTRAINT_CLEARING_PER_CURRENCY:
            existing = await _find_clearing_account(session, payload.currency)
            raise ClearingAccountExists(
                f"a clearing account for {payload.currency} already exists",
                currency=payload.currency,
                existing_account_id=existing,
            ) from exc
        raise

    await session.commit()
    response.headers["Location"] = f"/v1/accounts/{row.id}"
    return AccountRead(
        id=row.id,
        name=row.name,
        type=row.type,
        currency=row.currency,
        allow_negative=row.allow_negative,
        is_suspense=row.is_suspense,
        is_clearing=row.is_clearing,
        created_at=row.created_at,
        balance=0,
        entry_count=0,
    )


@router.get("/accounts/{account_id}", response_model=AccountRead)
async def get_account(account_id: uuid.UUID, session: SessionDep) -> AccountRead:
    row = (
        await session.execute(
            select(
                Account.id,
                Account.name,
                Account.type,
                Account.currency,
                Account.allow_negative,
                Account.is_suspense,
                Account.is_clearing,
                Account.created_at,
                AccountBalance.balance,
                AccountBalance.entry_count,
            )
            .join(AccountBalance, AccountBalance.account_id == Account.id)
            .where(Account.id == account_id)
        )
    ).one_or_none()
    if row is None:
        raise AccountNotFound(f"account {account_id} not found", account_id=account_id)
    return AccountRead(
        id=row.id,
        name=row.name,
        type=row.type,
        currency=row.currency,
        allow_negative=row.allow_negative,
        is_suspense=row.is_suspense,
        is_clearing=row.is_clearing,
        created_at=row.created_at,
        balance=row.balance,
        entry_count=row.entry_count,
    )


@router.get("/accounts/{account_id}/entries", response_model=Page[EntryRead])
async def list_account_entries(
    account_id: uuid.UUID, session: SessionDep, limit: int = 50, cursor: str | None = None
) -> Page[EntryRead]:
    exists = (
        await session.execute(select(Account.id).where(Account.id == account_id))
    ).scalar_one_or_none()
    if exists is None:
        raise AccountNotFound(f"account {account_id} not found", account_id=account_id)

    if not (1 <= limit <= 200):
        raise InvalidCursor("limit must be between 1 and 200")

    stmt = select(
        Entry.id,
        Entry.transaction_id,
        Entry.account_id,
        Entry.direction,
        Entry.amount,
        Entry.currency,
        Entry.created_at,
    ).where(Entry.account_id == account_id)
    if cursor is not None:
        c = decode_cursor(cursor)
        stmt = stmt.where(
            (Entry.created_at < c.created_at)
            | ((Entry.created_at == c.created_at) & (Entry.id < c.id))
        )
    stmt = stmt.order_by(Entry.created_at.desc(), Entry.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        EntryRead(
            id=r.id,
            transaction_id=r.transaction_id,
            account_id=r.account_id,
            direction=r.direction,
            amount=r.amount,
            currency=r.currency,
            created_at=r.created_at,
        )
        for r in rows
    ]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return Page[EntryRead](items=items, next_cursor=next_cursor, has_more=has_more)
