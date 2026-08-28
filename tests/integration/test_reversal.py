import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledger.core.errors import AlreadyReversed, InsufficientFunds, TransactionNotFound
from ledger.core.posting import EntryRequest, post_transaction, reverse_transaction
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection, TransactionStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.transactions import Transaction
from tests.conftest import AccountSnapshot

pytestmark = pytest.mark.integration

AccountFactory = Callable[..., Awaitable[AccountSnapshot]]


async def _balance(session: AsyncSession, account_id: uuid.UUID) -> tuple[int, int]:
    row = (
        await session.execute(
            select(AccountBalance.balance, AccountBalance.entry_count).where(
                AccountBalance.account_id == account_id
            )
        )
    ).one()
    return row.balance, row.entry_count


async def test_reverse_creates_mirrored_transaction(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 500, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 500, "USD"),
        ],
        external_ref="order-123",
    )
    await db_session.commit()

    reversal = await reverse_transaction(db_session, original.id)
    await db_session.commit()

    assert reversal.reversal_of == original.id
    assert reversal.external_ref is None
    assert reversal.description == f"Reversal of {original.id}"
    assert reversal.status is TransactionStatus.POSTED

    by_direction = {e.account_id: e.direction for e in reversal.entries}
    assert by_direction[cash.id] is EntryDirection.CREDIT
    assert by_direction[revenue.id] is EntryDirection.DEBIT


async def test_reverse_marks_original_reversed(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    await db_session.commit()

    await reverse_transaction(db_session, original.id)
    await db_session.commit()

    status = (
        await db_session.execute(select(Transaction.status).where(Transaction.id == original.id))
    ).scalar_one()
    assert status is TransactionStatus.REVERSED


async def test_reverse_restores_balances(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 250, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 250, "USD"),
        ],
    )
    await db_session.commit()

    await reverse_transaction(db_session, original.id)
    await db_session.commit()

    cash_balance, cash_count = await _balance(db_session, cash.id)
    rev_balance, rev_count = await _balance(db_session, revenue.id)
    assert cash_balance == 0
    assert rev_balance == 0
    assert cash_count == 2
    assert rev_count == 2


async def test_reverse_twice_raises_already_reversed(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    await db_session.commit()
    await reverse_transaction(db_session, original.id)
    await db_session.commit()

    with pytest.raises(AlreadyReversed):
        await reverse_transaction(db_session, original.id)


async def test_reverse_unknown_id_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(TransactionNotFound):
        await reverse_transaction(db_session, uuid.uuid4())


async def test_reverse_of_reversal_is_allowed(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    await db_session.commit()
    reversal = await reverse_transaction(db_session, original.id)
    await db_session.commit()

    # A reversal is itself a normal posted transaction; reversing it is a
    # legitimate "undo the undo" and nets back to the original state.
    undo = await reverse_transaction(db_session, reversal.id)
    await db_session.commit()
    assert undo.reversal_of == reversal.id

    cash_balance, cash_count = await _balance(db_session, cash.id)
    assert cash_balance == 100
    assert cash_count == 3


async def test_reverse_writes_outbox_event(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 10, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 10, "USD"),
        ],
    )
    await db_session.commit()
    await reverse_transaction(db_session, original.id)
    await db_session.commit()

    event_types = (
        (await db_session.execute(select(OutboxEvent.event_type).order_by(OutboxEvent.created_at)))
        .scalars()
        .all()
    )
    assert event_types.count("transaction.posted") == 2
    assert event_types.count("transaction.reversed") == 1


async def test_reversal_respects_insufficient_funds(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD", allow_negative=False)
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")

    # Fund cash to exactly 100, then spend it all elsewhere so reversing the
    # funding transaction would drive cash negative.
    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    await post_transaction(
        db_session,
        [
            EntryRequest(revenue.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(cash.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    await db_session.commit()

    with pytest.raises(InsufficientFunds):
        await reverse_transaction(db_session, original.id)

    status = (
        await db_session.execute(select(Transaction.status).where(Transaction.id == original.id))
    ).scalar_one()
    assert status is TransactionStatus.POSTED


async def test_reverse_multi_leg_transaction(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD")
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    tax = await account_factory(type=AccountType.LIABILITY, currency="USD")

    original = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 110, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
            EntryRequest(tax.id, EntryDirection.CREDIT, 10, "USD"),
        ],
    )
    await db_session.commit()
    await reverse_transaction(db_session, original.id)
    await db_session.commit()

    for account_id in (cash.id, revenue.id, tax.id):
        balance, _ = await _balance(db_session, account_id)
        assert balance == 0


async def test_concurrent_reverse_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
    usd_accounts: tuple[AccountSnapshot, AccountSnapshot],
) -> None:
    cash, revenue = usd_accounts
    async with session_factory() as setup:
        original = await post_transaction(
            setup,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
            ],
        )
        await setup.commit()

    async def attempt() -> str:
        async with session_factory() as s:
            try:
                await reverse_transaction(s, original.id)
                await s.commit()
                return "ok"
            except AlreadyReversed:
                await s.rollback()
                return "already_reversed"

    results = await asyncio.gather(*[attempt() for _ in range(10)])
    assert results.count("ok") == 1
    assert results.count("already_reversed") == 9

    async with session_factory() as check:
        count = (
            (
                await check.execute(
                    select(Transaction.id).where(Transaction.reversal_of == original.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1
