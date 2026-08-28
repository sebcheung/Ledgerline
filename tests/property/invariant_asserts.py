"""Raw-SQL invariant checks, deliberately independent of
`ledger.core.invariants` -- a property test that called the production
verifier to check the production verifier would prove nothing. Every
assertion here recomputes from `entries`/`accounts` directly.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.db import row_mappings

_SIGN_CASE = """
    CASE
      WHEN a.type IN ('asset', 'expense') AND e.direction = 'debit'  THEN  e.amount
      WHEN a.type IN ('asset', 'expense') AND e.direction = 'credit' THEN -e.amount
      WHEN a.type NOT IN ('asset', 'expense') AND e.direction = 'credit' THEN e.amount
      ELSE -e.amount
    END
"""


async def assert_every_transaction_balanced(session: AsyncSession) -> None:
    """Invariant 1."""
    rows = (
        await session.execute(
            text(
                """
                SELECT transaction_id, currency,
                       SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) AS net
                FROM entries
                GROUP BY transaction_id, currency
                HAVING SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) != 0
                """
            )
        )
    ).all()
    assert not rows, f"unbalanced transactions found: {row_mappings(rows)}"


async def assert_derivable(session: AsyncSession) -> None:
    """Invariant 3, every account."""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT a.id, ab.balance AS stored_balance, ab.entry_count AS stored_count,
                       COALESCE(SUM({_SIGN_CASE}), 0) AS derived_balance,
                       COUNT(e.id) AS derived_count
                FROM accounts a
                JOIN account_balances ab ON ab.account_id = a.id
                LEFT JOIN entries e ON e.account_id = a.id
                GROUP BY a.id, ab.balance, ab.entry_count
                HAVING ab.balance != COALESCE(SUM({_SIGN_CASE}), 0)
                    OR ab.entry_count != COUNT(e.id)
                """
            )
        )
    ).all()
    assert not rows, f"non-derivable accounts found: {row_mappings(rows)}"


async def assert_currency_consistent(session: AsyncSession) -> None:
    """Invariant 4."""
    rows = (
        await session.execute(
            text(
                """
                SELECT e.id, e.currency AS entry_currency, a.currency AS account_currency
                FROM entries e JOIN accounts a ON a.id = e.account_id
                WHERE e.currency != a.currency
                """
            )
        )
    ).all()
    assert not rows, f"entries with mismatched currency: {row_mappings(rows)}"

    rows2 = (
        await session.execute(
            text(
                """
                SELECT ab.account_id, ab.currency AS balance_currency,
                       a.currency AS account_currency
                FROM account_balances ab JOIN accounts a ON a.id = ab.account_id
                WHERE ab.currency != a.currency
                """
            )
        )
    ).all()
    assert not rows2, f"balances with mismatched currency: {row_mappings(rows2)}"


async def assert_no_illegal_negative(session: AsyncSession) -> None:
    """Invariant 5."""
    rows = (
        await session.execute(
            text(
                """
                SELECT ab.account_id, ab.balance
                FROM account_balances ab JOIN accounts a ON a.id = ab.account_id
                WHERE a.allow_negative = false AND ab.balance < 0
                """
            )
        )
    ).all()
    assert not rows, f"illegally negative balances found: {row_mappings(rows)}"


async def assert_global_balance(session: AsyncSession) -> None:
    """Invariant 7, grouped per currency."""
    rows = (
        await session.execute(
            text(
                """
                SELECT currency,
                       SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) AS net
                FROM entries
                GROUP BY currency
                HAVING SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) != 0
                """
            )
        )
    ).all()
    assert not rows, f"global imbalance found: {row_mappings(rows)}"


async def assert_all_invariants(session: AsyncSession) -> None:
    await assert_every_transaction_balanced(session)
    await assert_derivable(session)
    await assert_currency_consistent(session)
    await assert_no_illegal_negative(session)
    await assert_global_balance(session)
