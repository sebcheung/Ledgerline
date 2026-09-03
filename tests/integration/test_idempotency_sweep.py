"""Integration tests for `ledger.core.idempotency.sweep_idempotency_keys`
(SPEC.md §9 Phase 7)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.idempotency import sweep_idempotency_keys
from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey

pytestmark = pytest.mark.integration


async def _insert_key(
    session: AsyncSession, *, key: str, status: IdempotencyStatus, age_days: int
) -> None:
    created_at = datetime.now(UTC) - timedelta(days=age_days)
    await session.execute(
        insert(IdempotencyKey).values(
            key=key,
            endpoint="POST /v1/transactions",
            request_fingerprint="fp",
            status=status,
            locked_at=created_at,
            created_at=created_at,
        )
    )


async def test_sweep_deletes_old_completed_keys(db_session: AsyncSession) -> None:
    await _insert_key(db_session, key="old", status=IdempotencyStatus.COMPLETED, age_days=31)
    await db_session.commit()

    deleted = await sweep_idempotency_keys(db_session, older_than_days=30)
    await db_session.commit()

    assert deleted == 1
    remaining = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "old"))
    ).first()
    assert remaining is None


async def test_sweep_keeps_recent_completed_keys(db_session: AsyncSession) -> None:
    await _insert_key(db_session, key="recent", status=IdempotencyStatus.COMPLETED, age_days=1)
    await db_session.commit()

    deleted = await sweep_idempotency_keys(db_session, older_than_days=30)
    await db_session.commit()

    assert deleted == 0
    remaining = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "recent"))
    ).first()
    assert remaining is not None


async def test_sweep_never_deletes_in_progress_keys_regardless_of_age(
    db_session: AsyncSession,
) -> None:
    await _insert_key(db_session, key="stuck", status=IdempotencyStatus.IN_PROGRESS, age_days=365)
    await db_session.commit()

    deleted = await sweep_idempotency_keys(db_session, older_than_days=30)
    await db_session.commit()

    assert deleted == 0
    remaining = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "stuck"))
    ).first()
    assert remaining is not None


async def test_sweep_does_not_commit(db_session: AsyncSession) -> None:
    """SPEC.md §13's caller-commits rule: the caller (ledger.admin.sweep)
    owns the transaction boundary, so a caller that rolls back must see
    the delete undone."""
    await _insert_key(
        db_session, key="rolled-back", status=IdempotencyStatus.COMPLETED, age_days=31
    )
    await db_session.commit()

    deleted = await sweep_idempotency_keys(db_session, older_than_days=30)
    assert deleted == 1
    await db_session.rollback()

    remaining = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "rolled-back"))
    ).first()
    assert remaining is not None
