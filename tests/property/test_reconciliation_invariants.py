"""Property test: for any ledger built from `tests/property/strategies.py`'s
generator, running a reconciliation over a randomly-drifted feed of it
must never break invariants 1/3/4/5/7 -- the strongest single guard that
auto-resolution can't corrupt the ledger.

Reuses `assert_all_invariants` (raw-SQL, independent of
`ledger.core.invariants`, same convention as
`tests/property/test_ledger_invariants.py`) and the same
`generate_feed`/`DriftConfig` drift injection `ledger.reconciliation.feed`
provides, which `scripts/gen_feed.py`'s CLI and the fault suite both use.
"""

import random
import uuid
from collections.abc import Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.errors import CurrencyMismatch, InsufficientFunds, UnbalancedTransaction
from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.reconciliation.feed import DriftConfig, FeedTransaction, generate_feed
from ledger.reconciliation.ingest import ingest_batch
from ledger.reconciliation.runner import execute_run
from ledger.schemas.settlements import SettlementLineIn
from tests.property.invariant_asserts import assert_all_invariants
from tests.property.strategies import AccountSpec, PostOp, account_specs, post_ops
from tests.support.db import truncate_all

pytestmark = pytest.mark.integration


@st.composite
def recon_scenarios(
    draw: st.DrawFn,
) -> tuple[tuple[AccountSpec, ...], list[PostOp], DriftConfig, int]:
    accounts = draw(account_specs(min_size=2, max_size=3))
    ops = draw(st.lists(post_ops(accounts), min_size=1, max_size=5))
    drift = DriftConfig(
        drop_rate=draw(st.floats(min_value=0.0, max_value=0.5)),
        duplicate_rate=draw(st.floats(min_value=0.0, max_value=0.3)),
        perturb_rate=draw(st.floats(min_value=0.0, max_value=0.5)),
        perturb_max_minor=draw(st.integers(min_value=0, max_value=2000)),
        extra_lines=draw(st.integers(min_value=0, max_value=3)),
    )
    seed = draw(st.integers(min_value=0, max_value=2**31 - 1))
    return accounts, ops, drift, seed


async def _seed_accounts(
    session_factory: async_sessionmaker[AsyncSession], specs: Sequence[AccountSpec]
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    currencies = {spec.currency for spec in specs}
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

        # A suspense + clearing account per currency in play -- required
        # for the resolver's auto-resolve paths to have somewhere to post
        # to (see docs/DECISIONS.md Phase 4: allow_negative=true is an
        # operational requirement, not a resolver code path).
        for currency in currencies:
            for is_suspense, is_clearing in ((True, False), (False, True)):
                acct_row = (
                    await session.execute(
                        insert(Account)
                        .values(
                            name="suspense-or-clearing",
                            type="asset",
                            currency=currency,
                            allow_negative=True,
                            is_suspense=is_suspense,
                            is_clearing=is_clearing,
                        )
                        .returning(Account.id)
                    )
                ).one()
                await session.execute(
                    insert(AccountBalance).values(
                        account_id=acct_row.id, currency=currency, balance=0, entry_count=0
                    )
                )
        await session.commit()
    return ids


@settings(
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(scenario=recon_scenarios())
async def test_reconciliation_never_breaks_invariants(
    scenario: tuple[tuple[AccountSpec, ...], list[PostOp], DriftConfig, int],
    session_factory: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
    admin_engine: AsyncEngine,
) -> None:
    await truncate_all(admin_engine)
    account_specs_, ops, drift, seed = scenario
    account_ids = await _seed_accounts(session_factory, account_specs_)

    feed_txns: list[FeedTransaction] = []
    for i, op in enumerate(ops):
        async with session_factory() as session:
            entries = [
                EntryRequest(
                    account_id=account_ids[leg.account_index],
                    direction=leg.direction,
                    amount=leg.amount,
                    currency=op.currency,
                )
                for leg in op.legs
            ]
            try:
                posted = await post_transaction(session, entries, external_ref=f"prop-{seed}-{i}")
                await session.commit()
            except (InsufficientFunds, CurrencyMismatch, UnbalancedTransaction):
                # account_specs()/post_ops() can generate a leg against a
                # non-allow_negative account that this op would drive
                # negative -- an expected domain rejection, not a bug, and
                # exactly the same accepted case
                # tests/property/test_ledger_invariants.py wraps.
                await session.rollback()
                continue
            feed_txns.append(
                FeedTransaction(
                    external_ref=posted.external_ref,
                    amount=posted.total_debits,
                    currency=posted.currency,
                    value_date=posted.created_at.date(),
                )
            )

    generated = generate_feed(feed_txns, drift, random.Random(seed))
    async with session_factory() as session:
        await ingest_batch(
            session,
            [
                SettlementLineIn(
                    external_ref=line.external_ref,
                    amount=line.amount,
                    currency=line.currency,
                    value_date=line.value_date,
                    raw=line.raw,
                )
                for line in generated
            ],
        )
        await session.commit()

    async with session_factory() as session:
        await execute_run(session, db_engine)
        await session.commit()

    async with session_factory() as check_session:
        await assert_all_invariants(check_session)
