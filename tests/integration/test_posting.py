import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledger.core.errors import (
    AccountNotFound,
    CurrencyMismatch,
    DuplicateTransaction,
    InsufficientFunds,
    InvalidTransactionShape,
    UnbalancedTransaction,
)
from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection
from ledger.models.outbox import OutboxEvent
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


async def test_post_transaction_happy_path(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    posted = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 10_000, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 10_000, "USD"),
        ],
        description="test sale",
    )
    await db_session.commit()

    assert len(posted.entries) == 2
    assert posted.currency == "USD"
    assert posted.total_debits == 10_000

    cash_balance, cash_count = await _balance(db_session, cash.id)
    rev_balance, rev_count = await _balance(db_session, revenue.id)
    assert cash_balance == 10_000
    assert cash_count == 1
    assert rev_balance == 10_000
    assert rev_count == 1


async def test_post_transaction_writes_outbox_event(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    posted = await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 500, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 500, "USD"),
        ],
    )
    await db_session.commit()

    event = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "transaction.posted")
        )
    ).scalar_one()
    assert event.payload["transaction"]["id"] == str(posted.id)
    assert len(event.payload["entries"]) == 2
    assert "idempotency_key" not in event.payload["transaction"]


async def test_post_transaction_commits_and_is_visible_from_another_session(
    session_factory: async_sessionmaker[AsyncSession],
    usd_accounts: tuple[AccountSnapshot, AccountSnapshot],
) -> None:
    cash, revenue = usd_accounts
    async with session_factory() as s1:
        await post_transaction(
            s1,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 42, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 42, "USD"),
            ],
        )
        await s1.commit()

    async with session_factory() as s2:
        balance, count = await _balance(s2, cash.id)
        assert balance == 42
        assert count == 1


async def test_unbalanced_transaction_rejected_and_writes_nothing(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    with pytest.raises(UnbalancedTransaction):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 99, "USD"),
            ],
        )
    balance, count = await _balance(db_session, cash.id)
    assert (balance, count) == (0, 0)


async def test_insufficient_funds_rejected(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(
        name="Cash", type=AccountType.ASSET, currency="USD", allow_negative=False
    )
    revenue = await account_factory(name="Revenue", type=AccountType.REVENUE, currency="USD")

    with pytest.raises(InsufficientFunds):
        await post_transaction(
            db_session,
            [
                EntryRequest(revenue.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(cash.id, EntryDirection.CREDIT, 100, "USD"),
            ],
        )


async def test_allow_negative_account_can_go_negative(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    loose = await account_factory(
        name="Loose", type=AccountType.ASSET, currency="USD", allow_negative=True
    )
    # allow_negative=True on revenue too: it is only the balancing leg for
    # this test, and revenue's natural sign (debit decreases it) would
    # otherwise trip invariant 5 on the account we are *not* testing.
    revenue = await account_factory(
        name="Revenue", type=AccountType.REVENUE, currency="USD", allow_negative=True
    )

    await post_transaction(
        db_session,
        [
            EntryRequest(revenue.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(loose.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    # asset credit decreases the balance.
    balance, _ = await _balance(db_session, loose.id)
    assert balance == -100


async def test_exact_zero_boundary_succeeds(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD", allow_negative=False)
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    # Fund cash to exactly 100, then spend exactly 100 -- the debit that
    # lands cash at precisely 0 must succeed (< 0 is the rejection
    # boundary, not <= 0).
    await post_transaction(
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
    balance, _ = await _balance(db_session, cash.id)
    assert balance == 0


async def test_one_unit_over_boundary_fails(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD", allow_negative=False)
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    with pytest.raises(InsufficientFunds):
        await post_transaction(
            db_session,
            [
                EntryRequest(revenue.id, EntryDirection.DEBIT, 101, "USD"),
                EntryRequest(cash.id, EntryDirection.CREDIT, 101, "USD"),
            ],
        )


async def test_currency_mismatch_entry_vs_account(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    eur_cash = await account_factory(type=AccountType.ASSET, currency="EUR")
    usd_revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    with pytest.raises(CurrencyMismatch):
        await post_transaction(
            db_session,
            [
                EntryRequest(eur_cash.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(usd_revenue.id, EntryDirection.CREDIT, 100, "USD"),
            ],
        )


async def test_account_not_found(db_session: AsyncSession, account_factory: AccountFactory) -> None:
    cash = await account_factory()
    with pytest.raises(AccountNotFound):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(uuid.uuid4(), EntryDirection.CREDIT, 100, "USD"),
            ],
        )


async def test_single_entry_rejected(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory()
    with pytest.raises(InvalidTransactionShape):
        await post_transaction(
            db_session, [EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD")]
        )


async def test_empty_entries_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(InvalidTransactionShape):
        await post_transaction(db_session, [])


async def test_zero_amount_rejected(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    with pytest.raises(InvalidTransactionShape):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 0, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 0, "USD"),
            ],
        )


async def test_negative_amount_rejected(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    with pytest.raises(InvalidTransactionShape):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, -100, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, -100, "USD"),
            ],
        )


async def test_self_transfer_same_account_counts_twice(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(allow_negative=True)
    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
            EntryRequest(cash.id, EntryDirection.CREDIT, 100, "USD"),
        ],
    )
    balance, count = await _balance(db_session, cash.id)
    assert balance == 0
    assert count == 2


async def test_multi_leg_transaction(
    db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD")
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    tax = await account_factory(type=AccountType.LIABILITY, currency="USD")

    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 110, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
            EntryRequest(tax.id, EntryDirection.CREDIT, 10, "USD"),
        ],
    )
    cash_balance, _ = await _balance(db_session, cash.id)
    rev_balance, _ = await _balance(db_session, revenue.id)
    tax_balance, _ = await _balance(db_session, tax.id)
    assert cash_balance == 110
    assert rev_balance == 100
    assert tax_balance == 10


async def test_duplicate_idempotency_key_raises_and_session_stays_usable(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    cash, revenue = usd_accounts
    key = str(uuid.uuid4())
    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 10, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 10, "USD"),
        ],
        idempotency_key=key,
    )
    await db_session.commit()

    with pytest.raises(DuplicateTransaction):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 10, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 10, "USD"),
            ],
            idempotency_key=key,
        )

    # The SAVEPOINT means the session must still be usable after the
    # backstop fires -- prove it by running another statement.
    balance, _ = await _balance(db_session, cash.id)
    assert balance == 10


async def test_structured_constraint_detection_fires(
    db_session: AsyncSession, usd_accounts: tuple[AccountSnapshot, AccountSnapshot]
) -> None:
    from sqlalchemy import insert as sa_insert
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    from ledger.db.errors import constraint_name_of
    from ledger.models.enums import TransactionSource
    from ledger.models.transactions import Transaction

    cash, revenue = usd_accounts
    key = str(uuid.uuid4())
    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 10, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 10, "USD"),
        ],
        idempotency_key=key,
    )
    await db_session.commit()

    with pytest.raises(SAIntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                sa_insert(Transaction).values(idempotency_key=key, source=TransactionSource.API)
            )
    assert constraint_name_of(exc_info.value) == "transactions_idempotency_key_key"
