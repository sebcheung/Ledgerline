"""Ledger invariants (SPEC.md §4).

Enforcement table (see docs/ARCHITECTURE.md for the full write-up):

  1 Balance         -- assert_transaction_balanced, called pre-flush by posting
  2 Immutability     -- DB trigger entries_no_update; posting never touches
                        an ORM Entry entity, so autoflush cannot emit an
                        UPDATE against it
  3 Derivability     -- maintained by construction; verified here
  4 Currency         -- posting.py step 3, using the locked account row
  5 Sign constraint  -- posting.py step 5, against the *locked* balance
  6 Reversal symmetry -- by construction (exact mirrored entry set) +
                        uq_transactions_reversal_of
  7 Global balance   -- follows from 1 + 2; verified here
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import BigInteger, ColumnElement, Select, case, cast, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.errors import AccountNotFound, UnbalancedTransaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.entries import Entry
from ledger.models.enums import AccountType, EntryDirection

#: Account types for which a debit increases the balance (and a credit
#: decreases it). liability/equity/revenue are the mirror image. SPEC.md §5
#: step 4 -- this is the single place the sign rule is expressed; posting,
#: derivability, and the unit tests all call `signed_delta`.
DEBIT_POSITIVE_TYPES: frozenset[AccountType] = frozenset({AccountType.ASSET, AccountType.EXPENSE})


def signed_delta(account_type: AccountType, direction: EntryDirection, amount: int) -> int:
    """The signed effect of one entry on its account's balance."""
    positive = (direction is EntryDirection.DEBIT) == (account_type in DEBIT_POSITIVE_TYPES)
    return amount if positive else -amount


class _EntryLike(Protocol):
    """Structural type so this module doesn't need to import
    `ledger.core.posting.EntryRequest` (which would create a circular
    import -- posting.py imports invariants.py).

    Declared with read-only `@property` accessors rather than plain
    attribute annotations: mypy treats a Protocol's plain attributes as
    read-write, which does not structurally match a frozen dataclass's
    (read-only) attributes.
    """

    @property
    def direction(self) -> EntryDirection: ...

    @property
    def amount(self) -> int: ...

    @property
    def currency(self) -> str: ...


def assert_transaction_balanced(entries: Sequence[_EntryLike]) -> None:
    """Invariant 1: for a single-currency set of entries, debits == credits.

    Callers are expected to have already validated that every entry shares
    one currency (SPEC.md §5 step 1) -- this function trusts `entries[0]`'s
    currency label for the error message only and sums debits/credits
    regardless of currency, so a caller that skips the single-currency check
    could get a misleading "balanced" result. It is posting.py's job to
    validate shape before calling this.
    """
    debit_total = 0
    credit_total = 0
    for entry in entries:
        if entry.direction is EntryDirection.DEBIT:
            debit_total += entry.amount
        else:
            credit_total += entry.amount

    if debit_total != credit_total:
        currency = entries[0].currency if entries else ""
        raise UnbalancedTransaction(
            f"debits ({debit_total}) != credits ({credit_total})",
            debit_total=debit_total,
            credit_total=credit_total,
            currency=currency,
        )


def _debit_total_column() -> ColumnElement[int]:
    # Postgres's SUM(bigint) returns NUMERIC, not bigint (to avoid overflow
    # per the SQL standard) -- asyncpg maps that to Decimal, which the JSON
    # encoder then renders as a *string*. Cast back to BigInteger so this
    # stays a plain Python int all the way to the API response.
    return cast(
        func.coalesce(
            func.sum(case((Entry.direction == EntryDirection.DEBIT, Entry.amount), else_=0)), 0
        ),
        BigInteger,
    ).label("debit_total")


def _credit_total_column() -> ColumnElement[int]:
    return cast(
        func.coalesce(
            func.sum(case((Entry.direction == EntryDirection.CREDIT, Entry.amount), else_=0)), 0
        ),
        BigInteger,
    ).label("credit_total")


def _entry_count_column() -> ColumnElement[int]:
    return func.count(Entry.id).label("entry_count")


@dataclass(frozen=True, slots=True)
class DerivabilityReport:
    """Invariant 3 for one account: does the materialized balance equal a
    from-scratch recomputation from `entries`?"""

    account_id: uuid.UUID
    currency: str
    stored_balance: int
    derived_balance: int
    stored_entry_count: int
    derived_entry_count: int

    @property
    def ok(self) -> bool:
        return (
            self.stored_balance == self.derived_balance
            and self.stored_entry_count == self.derived_entry_count
        )


def _derivability_stmt() -> Select[tuple[uuid.UUID, AccountType, str, int, int, int, int, int]]:
    return (
        select(
            Account.id,
            Account.type,
            Account.currency,
            AccountBalance.balance,
            AccountBalance.entry_count,
            _debit_total_column(),
            _credit_total_column(),
            _entry_count_column(),
        )
        .select_from(Account)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .outerjoin(Entry, Entry.account_id == Account.id)
        .group_by(
            Account.id,
            Account.type,
            Account.currency,
            AccountBalance.balance,
            AccountBalance.entry_count,
        )
    )


async def verify_account_derivability(
    session: AsyncSession, account_id: uuid.UUID
) -> DerivabilityReport:
    """Invariant 3, single account. Recomputes the balance from `entries`
    from scratch -- never trusts `account_balances` beyond reading it for
    comparison. Read-only: no `FOR UPDATE`, so a concurrent posting can make
    this transiently disagree; callers wanting a stable read should call
    this only after quiescing posting activity (the concurrency tests join
    all posting tasks before asserting)."""
    stmt = _derivability_stmt().where(Account.id == account_id)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise AccountNotFound(f"account {account_id} not found", account_id=account_id)
    return _build_report(row)


async def verify_all_derivability(session: AsyncSession) -> tuple[DerivabilityReport, ...]:
    """Invariant 3, every account in one grouped query -- shares the SQL
    shape of `verify_account_derivability` to avoid N+1 in
    `GET /v1/admin/verify`."""
    stmt = _derivability_stmt().order_by(Account.id)
    rows = (await session.execute(stmt)).all()
    return tuple(_build_report(row) for row in rows)


def _build_report(
    row: "Row[tuple[uuid.UUID, AccountType, str, int, int, int, int, int]]",
) -> DerivabilityReport:
    # `Row` attribute access; the aggregate labels are unambiguous
    # (`debit_total`, `credit_total`, `entry_count`) because SQLAlchemy
    # deduplicates the *unlabelled* `AccountBalance.entry_count` column
    # against the labelled aggregate by suffixing it, so we read the
    # balance-table entry_count positionally instead of by name to avoid
    # relying on that suffixing behaviour.
    account_id = row[0]
    account_type = row[1]
    currency = row[2]
    stored_balance = row[3]
    stored_entry_count = row[4]
    debit_total = row[5]
    credit_total = row[6]
    derived_entry_count = row[7]
    derived_balance = signed_delta(account_type, EntryDirection.DEBIT, debit_total) + signed_delta(
        account_type, EntryDirection.CREDIT, credit_total
    )
    return DerivabilityReport(
        account_id=account_id,
        currency=currency,
        stored_balance=stored_balance,
        derived_balance=derived_balance,
        stored_entry_count=stored_entry_count,
        derived_entry_count=derived_entry_count,
    )


@dataclass(frozen=True, slots=True)
class CurrencyBalance:
    """Invariant 7 for one currency."""

    currency: str
    debit_total: int
    credit_total: int

    @property
    def ok(self) -> bool:
        return self.debit_total == self.credit_total


@dataclass(frozen=True, slots=True)
class GlobalBalanceReport:
    by_currency: tuple[CurrencyBalance, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.by_currency)


async def verify_global_balance(session: AsyncSession) -> GlobalBalanceReport:
    """Invariant 7: across the whole ledger, total debits == total credits,
    *per currency* -- grouping is mandatory, or a USD surplus could cancel
    an EUR deficit. Reversals are ordinary entries, so a reversed pair
    nets to zero and is already included."""
    stmt = (
        select(Entry.currency, _debit_total_column(), _credit_total_column())
        .group_by(Entry.currency)
        .order_by(Entry.currency)
    )
    rows = (await session.execute(stmt)).all()
    return GlobalBalanceReport(
        by_currency=tuple(
            CurrencyBalance(currency=row[0], debit_total=row[1], credit_total=row[2])
            for row in rows
        )
    )
