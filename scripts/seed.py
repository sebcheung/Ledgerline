"""Seed a database with accounts (including one suspense + one clearing
account per currency, both `allow_negative=true` -- an operational
requirement for reconciliation auto-resolution, see docs/DECISIONS.md
Phase 4) and a small, realistic transaction history."""

import asyncio
import uuid

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledger.core.posting import EntryRequest, post_transaction
from ledger.db.engine import engine
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection, TransactionSource

CURRENCIES = ("USD", "EUR")


async def _create_account(
    session: AsyncSession,
    *,
    name: str,
    type: AccountType,
    currency: str,
    allow_negative: bool = False,
    is_suspense: bool = False,
    is_clearing: bool = False,
) -> uuid.UUID:
    row = (
        await session.execute(
            insert(Account)
            .values(
                name=name,
                type=type,
                currency=currency,
                allow_negative=allow_negative,
                is_suspense=is_suspense,
                is_clearing=is_clearing,
            )
            .returning(Account.id)
        )
    ).one()
    account_id: uuid.UUID = row.id
    await session.execute(
        insert(AccountBalance).values(
            account_id=account_id, currency=currency, balance=0, entry_count=0
        )
    )
    return account_id


async def seed() -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        cash: dict[str, uuid.UUID] = {}
        revenue: dict[str, uuid.UUID] = {}
        for currency in CURRENCIES:
            cash[currency] = await _create_account(
                session, name=f"Cash ({currency})", type=AccountType.ASSET, currency=currency
            )
            revenue[currency] = await _create_account(
                session, name=f"Revenue ({currency})", type=AccountType.REVENUE, currency=currency
            )
            await _create_account(
                session,
                name=f"Suspense ({currency})",
                type=AccountType.ASSET,
                currency=currency,
                allow_negative=True,
                is_suspense=True,
            )
            await _create_account(
                session,
                name=f"Clearing ({currency})",
                type=AccountType.ASSET,
                currency=currency,
                allow_negative=True,
                is_clearing=True,
            )
        await session.commit()

        for i in range(20):
            currency = CURRENCIES[i % len(CURRENCIES)]
            amount = 1_000 + i * 137
            await post_transaction(
                session,
                [
                    EntryRequest(cash[currency], EntryDirection.DEBIT, amount, currency),
                    EntryRequest(revenue[currency], EntryDirection.CREDIT, amount, currency),
                ],
                external_ref=f"seed-{i}",
                description=f"Seed transaction {i}",
                source=TransactionSource.API,
            )
            await session.commit()

    await engine.dispose()
    print("seed complete")


if __name__ == "__main__":
    asyncio.run(seed())
