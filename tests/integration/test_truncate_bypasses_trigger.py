"""Pins the one fact the whole test-isolation design leans on: `entries_no_update`
is a row-level `BEFORE UPDATE OR DELETE` trigger, so it does not intercept
`TRUNCATE` (which is neither). If a future migration ever adds a
`BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger, this test breaks loudly
instead of the entire suite mysteriously hanging on cleanup.
"""

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.models.accounts import Account
from ledger.models.enums import AccountType
from tests.support.db import truncate_all

pytestmark = pytest.mark.integration


async def test_truncate_bypasses_append_only_trigger(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    await db_session.execute(
        insert(Account).values(
            name="truncate-test", type=AccountType.ASSET, currency="USD", allow_negative=False
        )
    )
    await db_session.commit()

    await truncate_all(db_engine)

    count = (await db_session.execute(text("SELECT COUNT(*) FROM accounts"))).scalar_one()
    assert count == 0
