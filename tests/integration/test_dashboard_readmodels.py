"""Integration tests for `ledger.readmodels` (SPEC.md §12 Phase 6) -- the
dashboard's query layer, exercised directly against a real database (no
HTTP; `tests/integration/test_dashboard_views.py` covers the routes)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.enums import (
    AccountType,
    EntryDirection,
    TransactionSource,
    WebhookDeliveryStatus,
)
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint
from ledger.readmodels.balances import load_balances
from ledger.readmodels.transactions import load_recent_transactions
from ledger.readmodels.webhooks import load_delivery_queue, load_queue_summary
from tests.conftest import AccountSnapshot

pytestmark = pytest.mark.integration


async def _insert_endpoint(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        row = (
            await session.execute(
                insert(WebhookEndpoint)
                .values(url="http://127.0.0.1:9/hook", secret="s", active=True)
                .returning(WebhookEndpoint.id)
            )
        ).one()
        await session.commit()
        return uuid.UUID(str(row.id))


async def _insert_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    endpoint_id: uuid.UUID,
    status: WebhookDeliveryStatus,
    next_attempt_at: datetime,
) -> uuid.UUID:
    async with session_factory() as session:
        event_row = (
            await session.execute(
                insert(OutboxEvent)
                .values(event_type="transaction.posted", payload={})
                .returning(OutboxEvent.id)
            )
        ).one()
        delivery_row = (
            await session.execute(
                insert(WebhookDelivery)
                .values(
                    event_id=event_row.id,
                    endpoint_id=endpoint_id,
                    status=status,
                    next_attempt_at=next_attempt_at,
                )
                .returning(WebhookDelivery.id)
            )
        ).one()
        await session.commit()
        return uuid.UUID(str(delivery_row.id))


async def test_balance_totals_group_per_currency(
    db_session: AsyncSession, account_factory: Callable[..., Awaitable[AccountSnapshot]]
) -> None:
    usd_cash = await account_factory(name="USD Cash", currency="USD")
    usd_revenue = await account_factory(
        name="USD Revenue", type=AccountType.REVENUE, currency="USD"
    )
    eur_cash = await account_factory(name="EUR Cash", currency="EUR")
    eur_revenue = await account_factory(
        name="EUR Revenue", type=AccountType.REVENUE, currency="EUR"
    )

    await post_transaction(
        db_session,
        [
            EntryRequest(usd_cash.id, EntryDirection.DEBIT, 1_000, "USD"),
            EntryRequest(usd_revenue.id, EntryDirection.CREDIT, 1_000, "USD"),
        ],
        source=TransactionSource.API,
    )
    await post_transaction(
        db_session,
        [
            EntryRequest(eur_cash.id, EntryDirection.DEBIT, 500, "EUR"),
            EntryRequest(eur_revenue.id, EntryDirection.CREDIT, 500, "EUR"),
        ],
        source=TransactionSource.API,
    )
    await db_session.commit()

    snapshot = await load_balances(db_session)
    totals = {t.currency: t.total for t in snapshot.totals_by_currency}
    # USD cash (+1000, debit-normal asset) and USD revenue (+1000,
    # credit-normal revenue) sum to 2000 -- what matters here is that EUR's
    # 1000 total never leaks into USD's, not the sign convention itself.
    assert totals == {"USD": 2_000, "EUR": 1_000}
    assert {a.currency for a in snapshot.accounts} == {"USD", "EUR"}


async def test_recent_transactions_ordering_is_stable_across_calls(
    db_session: AsyncSession, account_factory: Callable[..., Awaitable[AccountSnapshot]]
) -> None:
    cash = await account_factory(name="Cash")
    revenue = await account_factory(name="Revenue", type=AccountType.REVENUE)
    for i in range(5):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 100 + i, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 100 + i, "USD"),
            ],
            external_ref=f"txn-{i}",
            source=TransactionSource.API,
        )
        # Commit between postings: each is then its own DB transaction with
        # its own `now()`, so `created_at` actually differs -- otherwise all
        # five would share one timestamp (Postgres `now()` is
        # transaction-start time) and the id tiebreak would decide the
        # order instead of insertion order, making this test flaky by
        # construction.
        await db_session.commit()

    first = await load_recent_transactions(db_session, 10)
    second = await load_recent_transactions(db_session, 10)
    assert [r.id for r in first] == [r.id for r in second]
    assert len(first) == 5
    assert first[0].amount == 104  # most recently posted


async def test_recent_transactions_respects_limit(
    db_session: AsyncSession, account_factory: Callable[..., Awaitable[AccountSnapshot]]
) -> None:
    cash = await account_factory(name="Cash")
    revenue = await account_factory(name="Revenue", type=AccountType.REVENUE)
    for _i in range(3):
        await post_transaction(
            db_session,
            [
                EntryRequest(cash.id, EntryDirection.DEBIT, 100, "USD"),
                EntryRequest(revenue.id, EntryDirection.CREDIT, 100, "USD"),
            ],
            source=TransactionSource.API,
        )
    await db_session.commit()

    rows = await load_recent_transactions(db_session, 2)
    assert len(rows) == 2


async def test_queue_summary_counts_every_status_exactly(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    endpoint_id = await _insert_endpoint(session_factory)
    now = datetime.now(UTC)
    await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.PENDING,
        next_attempt_at=now,
    )
    await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.DEAD,
        next_attempt_at=now,
    )
    await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.DEAD,
        next_attempt_at=now,
    )
    await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.SUCCEEDED,
        next_attempt_at=now,
    )

    summary = await load_queue_summary(db_session)
    assert summary.counts == {
        WebhookDeliveryStatus.PENDING: 1,
        WebhookDeliveryStatus.DELIVERING: 0,
        WebhookDeliveryStatus.SUCCEEDED: 1,
        WebhookDeliveryStatus.DEAD: 2,
    }
    assert summary.dlq_depth == 2


async def test_queue_summary_dlq_depth_is_zero_not_none_when_empty(
    db_session: AsyncSession,
) -> None:
    summary = await load_queue_summary(db_session)
    assert summary.dlq_depth == 0
    assert summary.counts == dict.fromkeys(WebhookDeliveryStatus, 0)


async def test_seconds_until_retry_uses_the_db_clock(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
) -> None:
    endpoint_id = await _insert_endpoint(session_factory)
    delivery_id = await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.PENDING,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=100),
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE webhook_deliveries"
                " SET next_attempt_at = now() + make_interval(secs => :s)"
                " WHERE id = :id"
            ),
            {"s": 30, "id": delivery_id},
        )

    rows = await load_delivery_queue(db_session, 50)
    row = next(r for r in rows if r.id == delivery_id)
    assert row.seconds_until_retry == 30
    assert row.claim_is_stale is False


async def test_delivery_queue_flags_overdue_rows_as_due(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    endpoint_id = await _insert_endpoint(session_factory)
    delivery_id = await _insert_delivery(
        session_factory,
        endpoint_id=endpoint_id,
        status=WebhookDeliveryStatus.PENDING,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    rows = await load_delivery_queue(db_session, 50)
    row = next(r for r in rows if r.id == delivery_id)
    assert row.seconds_until_retry is not None
    assert row.seconds_until_retry <= 0
