"""Concurrency and deadlock-order tests (SPEC.md §10).

SPEC.md says "50 threads"; the property that actually matters is 50
simultaneous *open Postgres transactions* contending for one
`account_balances` row, not literally 50 OS threads. asyncpg connections
are bound to the event loop they were created on, so 50 real threads would
mean either 50 separate event loops (legal, but pointless -- it buys
nothing a single loop with 50 tasks doesn't already give us) or a sync
driver this project doesn't install. `asyncio.gather` over 50 tasks, each
with its own connection from a `NullPool` engine, gives 50 genuinely
independent Postgres backends and transactions -- which is the substitution
recorded in docs/DECISIONS.md.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.errors import InsufficientFunds
from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.timeout(120)]

CONCURRENCY = 50


async def _make_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    allow_negative: bool,
    account_type: AccountType,
) -> uuid.UUID:
    async with session_factory() as session:
        row = (
            await session.execute(
                insert(Account)
                .values(
                    name="concurrency-test",
                    type=account_type,
                    currency="USD",
                    allow_negative=allow_negative,
                )
                .returning(Account.id)
            )
        ).one()
        await session.execute(
            insert(AccountBalance).values(
                account_id=row.id, currency="USD", balance=0, entry_count=0
            )
        )
        await session.commit()
        account_id: uuid.UUID = row.id
        return account_id


async def _backend_pid(session: AsyncSession) -> int:
    pid: int = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    return pid


async def test_fifty_concurrent_postings_exact_balance(
    concurrency_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    cash = await _make_account(session_factory, allow_negative=True, account_type=AccountType.ASSET)
    revenue = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.REVENUE
    )

    backend_pids: set[int] = set()
    barrier = asyncio.Barrier(CONCURRENCY)

    async def worker() -> None:
        async with session_factory() as session:
            backend_pids.add(await _backend_pid(session))
            await session.execute(text("SET LOCAL lock_timeout = '10s'"))
            await barrier.wait()
            await post_transaction(
                session,
                [
                    EntryRequest(cash, EntryDirection.DEBIT, 100, "USD"),
                    EntryRequest(revenue, EntryDirection.CREDIT, 100, "USD"),
                ],
            )
            await session.commit()

    results = await asyncio.wait_for(
        asyncio.gather(*[worker() for _ in range(CONCURRENCY)], return_exceptions=True), timeout=60
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == []

    # Without this, a pool misconfiguration could silently serialize every
    # task onto one connection and the test would pass while proving
    # nothing about real contention.
    assert len(backend_pids) == CONCURRENCY

    async with session_factory() as session:
        cash_row = (
            await session.execute(
                select(AccountBalance.balance, AccountBalance.entry_count).where(
                    AccountBalance.account_id == cash
                )
            )
        ).one()
        rev_row = (
            await session.execute(
                select(AccountBalance.balance, AccountBalance.entry_count).where(
                    AccountBalance.account_id == revenue
                )
            )
        ).one()
        entries_count = (await session.execute(text("SELECT COUNT(*) FROM entries"))).scalar_one()
        txn_count = (await session.execute(text("SELECT COUNT(*) FROM transactions"))).scalar_one()
        event_count = (
            await session.execute(text("SELECT COUNT(*) FROM outbox_events"))
        ).scalar_one()

    assert cash_row.balance == CONCURRENCY * 100
    assert cash_row.entry_count == CONCURRENCY
    # revenue credit increases its balance.
    assert rev_row.balance == CONCURRENCY * 100
    assert rev_row.entry_count == CONCURRENCY
    assert entries_count == CONCURRENCY * 2
    assert txn_count == CONCURRENCY
    assert event_count == CONCURRENCY


async def test_fifty_concurrent_debits_insufficient_funds(concurrency_engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    cash = await _make_account(
        session_factory, allow_negative=False, account_type=AccountType.ASSET
    )
    revenue = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.REVENUE
    )

    # Fund cash to exactly 30 units (debit cash, credit revenue -- both
    # increase), then hit it with 50 concurrent spends of 1 unit each
    # (credit cash, debit revenue -- both decrease) -- exactly 30 must
    # succeed and exactly 20 must fail, deterministically, under correct
    # locking.
    async with session_factory() as session:
        await post_transaction(
            session,
            [
                EntryRequest(cash, EntryDirection.DEBIT, 30, "USD"),
                EntryRequest(revenue, EntryDirection.CREDIT, 30, "USD"),
            ],
        )
        await session.commit()

    barrier = asyncio.Barrier(CONCURRENCY)

    async def worker() -> str:
        async with session_factory() as session:
            await session.execute(text("SET LOCAL lock_timeout = '10s'"))
            await barrier.wait()
            try:
                await post_transaction(
                    session,
                    [
                        EntryRequest(revenue, EntryDirection.DEBIT, 1, "USD"),
                        EntryRequest(cash, EntryDirection.CREDIT, 1, "USD"),
                    ],
                )
                await session.commit()
                return "ok"
            except InsufficientFunds:
                await session.rollback()
                return "insufficient_funds"

    results = await asyncio.wait_for(
        asyncio.gather(*[worker() for _ in range(CONCURRENCY)]), timeout=60
    )
    assert results.count("ok") == 30
    assert results.count("insufficient_funds") == 20

    async with session_factory() as session:
        balance = (
            await session.execute(
                select(AccountBalance.balance).where(AccountBalance.account_id == cash)
            )
        ).scalar_one()
    assert balance == 0


async def test_opposite_order_requests_do_not_deadlock(concurrency_engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    account_a = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.ASSET
    )
    account_b = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.ASSET
    )

    pairs = 25
    total_tasks = pairs * 2
    barrier = asyncio.Barrier(total_tasks)

    async def lock_hook(_account_id: uuid.UUID) -> None:
        # Widens the window in which an incorrectly-ordered implementation
        # would be holding one lock and wanting the other, from
        # microseconds to 50ms.
        await asyncio.sleep(0.05)

    async def worker(first: uuid.UUID, second: uuid.UUID) -> None:
        async with session_factory() as session:
            await session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await barrier.wait()
            await post_transaction(
                session,
                [
                    EntryRequest(first, EntryDirection.DEBIT, 1, "USD"),
                    EntryRequest(second, EntryDirection.CREDIT, 1, "USD"),
                ],
                _lock_hook=lock_hook,
            )
            await session.commit()

    tasks = [worker(account_a, account_b) for _ in range(pairs)] + [
        worker(account_b, account_a) for _ in range(pairs)
    ]
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=90)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"unexpected errors (possible deadlock): {errors}"


async def test_control_unordered_locking_does_deadlock(concurrency_engine: AsyncEngine) -> None:
    """Positive control: without this, 'no deadlock observed' in the test
    above is unfalsifiable -- a harness that could never detect a deadlock
    would make that test pass trivially. This proves the harness *can*
    detect one by deliberately locking in request order (unsorted) instead
    of ascending account_id order."""
    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    account_a = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.ASSET
    )
    account_b = await _make_account(
        session_factory, allow_negative=True, account_type=AccountType.ASSET
    )

    pairs = 25
    total_tasks = pairs * 2
    barrier = asyncio.Barrier(total_tasks)

    async def worker(first: uuid.UUID, second: uuid.UUID) -> None:
        async with session_factory() as session:
            await session.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await barrier.wait()
            # Deliberately lock in *request* order, not ascending id order.
            await session.execute(
                select(AccountBalance.balance)
                .where(AccountBalance.account_id == first)
                .with_for_update(of=AccountBalance)
            )
            await asyncio.sleep(0.05)
            await session.execute(
                select(AccountBalance.balance)
                .where(AccountBalance.account_id == second)
                .with_for_update(of=AccountBalance)
            )
            await session.commit()

    tasks = [worker(account_a, account_b) for _ in range(pairs)] + [
        worker(account_b, account_a) for _ in range(pairs)
    ]
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=90)
    deadlocks = [
        r
        for r in results
        if isinstance(r, DBAPIError) and getattr(r.orig, "sqlstate", None) == "40P01"
    ]
    assert len(deadlocks) >= 1, "expected the unordered-locking control to deadlock at least once"
