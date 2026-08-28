import uuid
from collections.abc import Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.errors import (
    AccountNotFound,
    AlreadyReversed,
    CurrencyMismatch,
    InsufficientFunds,
    TransactionNotFound,
    UnbalancedTransaction,
)
from ledger.core.posting import EntryRequest, post_transaction, reverse_transaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from tests.property.invariant_asserts import assert_all_invariants
from tests.property.strategies import AccountSpec, PostOp, ReverseOp, Scenario, scenarios
from tests.support.db import truncate_all

pytestmark = pytest.mark.integration

_EXPECTED_DOMAIN_ERRORS = (
    UnbalancedTransaction,
    InsufficientFunds,
    CurrencyMismatch,
    AccountNotFound,
    AlreadyReversed,
    TransactionNotFound,
)


async def _seed_accounts(
    session_factory: async_sessionmaker[AsyncSession], specs: Sequence[AccountSpec]
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with session_factory() as session:
        for spec in specs:
            row = (
                await session.execute(
                    insert(Account)
                    .values(
                        name="scenario-account",
                        type=spec.type,
                        currency=spec.currency,
                        allow_negative=spec.allow_negative,
                    )
                    .returning(Account.id)
                )
            ).one()
            await session.execute(
                insert(AccountBalance).values(
                    account_id=row.id, currency=spec.currency, balance=0, entry_count=0
                )
            )
            ids.append(row.id)
        await session.commit()
    return ids


@settings(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(scenario=scenarios())
async def test_invariants_hold_after_every_operation(
    scenario: Scenario,
    session_factory: async_sessionmaker[AsyncSession],
    admin_engine: AsyncEngine,
) -> None:
    await truncate_all(admin_engine)
    account_specs, ops = scenario
    account_ids = await _seed_accounts(session_factory, account_specs)
    posted_ids: list[uuid.UUID] = []

    for op in ops:
        async with session_factory() as session:
            try:
                if isinstance(op, PostOp):
                    entries = [
                        EntryRequest(
                            account_id=account_ids[leg.account_index],
                            direction=leg.direction,
                            amount=leg.amount,
                            currency=op.currency,
                        )
                        for leg in op.legs
                    ]
                    posted = await post_transaction(session, entries)
                    await session.commit()
                    posted_ids.append(posted.id)
                elif isinstance(op, ReverseOp):
                    if not posted_ids:
                        continue
                    target = posted_ids[op.target_index % len(posted_ids)]
                    reversal = await reverse_transaction(session, target)
                    await session.commit()
                    posted_ids.append(reversal.id)
            except _EXPECTED_DOMAIN_ERRORS:
                await session.rollback()
                continue
            # A domain error must never surface as a database error --
            # anything else (IntegrityError, DBAPIError, AssertionError,
            # TypeError) propagates and fails the example.

        async with session_factory() as check_session:
            await assert_all_invariants(check_session)
