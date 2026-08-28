import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.posting import EntryRequest, post_transaction, reverse_transaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection
from tests.support.db import truncate_all

pytestmark = pytest.mark.integration


@st.composite
def two_leg_amounts(draw: st.DrawFn) -> int:
    return draw(st.integers(min_value=1, max_value=10_000))


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(amount=two_leg_amounts())
async def test_post_then_reverse_restores_all_balances(
    amount: int,
    session_factory: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    await truncate_all(admin_engine)
    async with session_factory() as session:
        cash_id = (
            (
                await session.execute(
                    insert(Account)
                    .values(
                        name="cash", type=AccountType.ASSET, currency="USD", allow_negative=True
                    )
                    .returning(Account.id)
                )
            )
            .one()
            .id
        )
        revenue_id = (
            (
                await session.execute(
                    insert(Account)
                    .values(
                        name="rev", type=AccountType.REVENUE, currency="USD", allow_negative=True
                    )
                    .returning(Account.id)
                )
            )
            .one()
            .id
        )
        await session.execute(
            insert(AccountBalance).values(
                account_id=cash_id, currency="USD", balance=0, entry_count=0
            )
        )
        await session.execute(
            insert(AccountBalance).values(
                account_id=revenue_id, currency="USD", balance=0, entry_count=0
            )
        )
        await session.commit()

    async def snapshot(session: AsyncSession, account_id: object) -> tuple[int, int]:
        row = (
            await session.execute(
                select(AccountBalance.balance, AccountBalance.entry_count).where(
                    AccountBalance.account_id == account_id
                )
            )
        ).one()
        return row.balance, row.entry_count

    async with session_factory() as session:
        before_cash = await snapshot(session, cash_id)
        before_rev = await snapshot(session, revenue_id)

        posted = await post_transaction(
            session,
            [
                EntryRequest(cash_id, EntryDirection.DEBIT, amount, "USD"),
                EntryRequest(revenue_id, EntryDirection.CREDIT, amount, "USD"),
            ],
        )
        await session.commit()

    async with session_factory() as session:
        await reverse_transaction(session, posted.id)
        await session.commit()

    async with session_factory() as session:
        after_cash = await snapshot(session, cash_id)
        after_rev = await snapshot(session, revenue_id)

    assert after_cash == (before_cash[0], before_cash[1] + 2)
    assert after_rev == (before_rev[0], before_rev[1] + 2)
