import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.accounts import Account
from ledger.models.entries import Entry
from ledger.models.enums import AccountType, EntryDirection, TransactionSource
from ledger.models.transactions import Transaction

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_update_on_entries_is_rejected(db_session: AsyncSession) -> None:
    account = Account(
        name="Cash",
        type=AccountType.ASSET,
        currency="USD",
        allow_negative=False,
    )
    db_session.add(account)
    await db_session.flush()

    transaction = Transaction(source=TransactionSource.API)
    db_session.add(transaction)
    await db_session.flush()

    entry = Entry(
        transaction_id=transaction.id,
        account_id=account.id,
        direction=EntryDirection.DEBIT,
        amount=100,
        currency="USD",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    with pytest.raises(DBAPIError, match="entries is append-only"):
        await db_session.execute(
            text("UPDATE entries SET amount = 200 WHERE id = :id"), {"id": entry_id}
        )
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="entries is append-only"):
        await db_session.execute(text("DELETE FROM entries WHERE id = :id"), {"id": entry_id})
    await db_session.rollback()
