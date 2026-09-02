"""Account balance read model for the dashboard's balances panel."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType


@dataclass(frozen=True, slots=True)
class AccountBalanceRow:
    id: uuid.UUID
    name: str
    type: AccountType
    currency: str
    allow_negative: bool
    is_suspense: bool
    is_clearing: bool
    balance: int
    entry_count: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    currency: str
    total: int


@dataclass(frozen=True, slots=True)
class BalancesSnapshot:
    accounts: list[AccountBalanceRow]
    totals_by_currency: list[CurrencyTotal]


async def load_balances(session: AsyncSession) -> BalancesSnapshot:
    """Every account joined to its balance, unbounded -- the account set is
    small and operator-meaningful in full (mirrors the join already used by
    `GET /v1/accounts/{id}`, ledger/api/routes/accounts.py). Per-currency
    totals are folded up in Python from these same rows: a second `GROUP BY`
    query for a row set this small buys nothing."""
    stmt = (
        select(
            Account.id,
            Account.name,
            Account.type,
            Account.currency,
            Account.allow_negative,
            Account.is_suspense,
            Account.is_clearing,
            AccountBalance.balance,
            AccountBalance.entry_count,
            AccountBalance.updated_at,
        )
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .order_by(Account.currency, Account.name, Account.id)
    )
    rows = (await session.execute(stmt)).all()

    accounts = [
        AccountBalanceRow(
            id=r.id,
            name=r.name,
            type=r.type,
            currency=r.currency,
            allow_negative=r.allow_negative,
            is_suspense=r.is_suspense,
            is_clearing=r.is_clearing,
            balance=r.balance,
            entry_count=r.entry_count,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    totals: dict[str, int] = {}
    for row in accounts:
        totals[row.currency] = totals.get(row.currency, 0) + row.balance
    totals_by_currency = [
        CurrencyTotal(currency=currency, total=total) for currency, total in sorted(totals.items())
    ]

    return BalancesSnapshot(accounts=accounts, totals_by_currency=totals_by_currency)
