import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledger.core.errors import (
    IdempotencyKeyReuse,
    IdempotencyKeyScopeConflict,
    IdempotencyStateError,
)
from ledger.core.idempotency import (
    ClaimOutcome,
    IdempotencyConflict,
    canonical_hash,
    claim_key,
    complete_key,
    load_key,
    release_key,
)
from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey

pytestmark = pytest.mark.integration

_ENDPOINT = "POST /v1/transactions"
_TTL = 30


async def _backdate(engine: AsyncEngine, key: str, seconds: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys"
                " SET locked_at = now() - make_interval(secs => :s)"
                " WHERE key = :k"
            ),
            {"s": seconds, "k": key},
        )


async def _status(session: AsyncSession, key: str) -> IdempotencyStatus:
    row = (
        await session.execute(select(IdempotencyKey.status).where(IdempotencyKey.key == key))
    ).one()
    return IdempotencyStatus(row.status)


async def test_fresh_claim_commits_and_is_visible_from_another_session(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    fp = canonical_hash(b'{"a": 1}')
    claim = await claim_key(
        db_session, key="k1", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    assert claim.outcome is ClaimOutcome.EXECUTE
    assert claim.locked_at is not None

    async with session_factory() as other:
        assert await _status(other, "k1") == IdempotencyStatus.IN_PROGRESS


async def test_conflict_on_fresh_in_progress_lock(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    await claim_key(db_session, key="k2", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL)

    with pytest.raises(IdempotencyConflict):
        await claim_key(
            db_session, key="k2", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
        )


async def test_conflict_carries_retry_after_header(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    await claim_key(
        db_session, key="k2b", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )

    with pytest.raises(IdempotencyConflict) as exc_info:
        await claim_key(
            db_session, key="k2b", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
        )
    assert exc_info.value.problem_headers() == {"Retry-After": "1"}


async def test_replay_on_completed_matching_fingerprint(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    claim = await claim_key(
        db_session, key="k3", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    await complete_key(db_session, key="k3", response_status=201, response_body={"id": "x"})
    await db_session.commit()

    replay_claim = await claim_key(
        db_session, key="k3", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    assert replay_claim.outcome is ClaimOutcome.REPLAY
    assert replay_claim.locked_at is None
    assert replay_claim.replay is not None
    assert replay_claim.replay.status == 201
    assert replay_claim.replay.body == {"id": "x"}
    assert claim.locked_at is not None  # sanity: original claim did succeed


async def test_key_reuse_on_completed_mismatched_fingerprint(db_session: AsyncSession) -> None:
    fp1 = canonical_hash(b'{"a": 1}')
    fp2 = canonical_hash(b'{"a": 2}')
    await claim_key(
        db_session, key="k4", endpoint=_ENDPOINT, fingerprint=fp1, lock_ttl_seconds=_TTL
    )
    await complete_key(db_session, key="k4", response_status=201, response_body={})
    await db_session.commit()

    with pytest.raises(IdempotencyKeyReuse):
        await claim_key(
            db_session, key="k4", endpoint=_ENDPOINT, fingerprint=fp2, lock_ttl_seconds=_TTL
        )


async def test_scope_conflict_on_different_endpoint(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    await claim_key(db_session, key="k5", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL)

    with pytest.raises(IdempotencyKeyScopeConflict):
        await claim_key(
            db_session,
            key="k5",
            endpoint="POST /v1/transactions/{transaction_id}/reverse",
            fingerprint=fp,
            lock_ttl_seconds=_TTL,
        )


async def test_stale_lock_reclaimed_with_matching_fingerprint(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    fp = canonical_hash(b'{"a": 1}')
    first = await claim_key(
        db_session, key="k6", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    await _backdate(db_engine, "k6", _TTL + 1)

    second = await claim_key(
        db_session, key="k6", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    assert second.outcome is ClaimOutcome.EXECUTE
    assert second.locked_at is not None
    assert first.locked_at is not None
    assert second.locked_at > first.locked_at


async def test_stale_lock_with_mismatched_fingerprint_is_key_reuse_not_reclaim(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    fp1 = canonical_hash(b'{"a": 1}')
    fp2 = canonical_hash(b'{"a": 2}')
    await claim_key(
        db_session, key="k7", endpoint=_ENDPOINT, fingerprint=fp1, lock_ttl_seconds=_TTL
    )
    await _backdate(db_engine, "k7", _TTL + 1)

    with pytest.raises(IdempotencyKeyReuse):
        await claim_key(
            db_session, key="k7", endpoint=_ENDPOINT, fingerprint=fp2, lock_ttl_seconds=_TTL
        )


async def test_two_concurrent_stale_reclaimers_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession], db_engine: AsyncEngine
) -> None:
    fp = canonical_hash(b'{"a": 1}')
    async with session_factory() as setup:
        await claim_key(setup, key="k8", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL)
    await _backdate(db_engine, "k8", _TTL + 1)

    async def attempt() -> ClaimOutcome | None:
        async with session_factory() as session:
            try:
                claim = await claim_key(
                    session, key="k8", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
                )
                return claim.outcome
            except IdempotencyConflict:
                return None

    outcomes = await asyncio.gather(attempt(), attempt())
    executes = [o for o in outcomes if o is ClaimOutcome.EXECUTE]
    conflicts = [o for o in outcomes if o is None]
    assert len(executes) == 1
    assert len(conflicts) == 1


async def test_complete_key_rowcount_zero_raises_state_error(db_session: AsyncSession) -> None:
    with pytest.raises(IdempotencyStateError):
        await complete_key(db_session, key="does-not-exist", response_status=201, response_body={})


async def test_release_key_is_fenced_on_locked_at(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    fp = canonical_hash(b'{"a": 1}')
    claim = await claim_key(
        db_session, key="k9", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    original_locked_at = claim.locked_at
    assert original_locked_at is not None

    # Simulate a concurrent reclaim bumping locked_at behind our back, then
    # try to release using the *stale* locked_at we're still holding.
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE idempotency_keys SET locked_at = now() WHERE key = 'k9'"))

    await release_key(db_session, key="k9", locked_at=original_locked_at)

    # The row must still exist -- our stale locked_at didn't match, so the
    # fenced DELETE affected nothing.
    async with db_engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT status FROM idempotency_keys WHERE key = 'k9'"))
        ).one_or_none()
    assert row is not None


async def test_release_key_deletes_when_locked_at_matches(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    claim = await claim_key(
        db_session, key="k10", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    assert claim.locked_at is not None

    await release_key(db_session, key="k10", locked_at=claim.locked_at)

    reclaimed = await claim_key(
        db_session, key="k10", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )
    assert reclaimed.outcome is ClaimOutcome.EXECUTE


async def test_load_key_returns_none_when_not_completed(db_session: AsyncSession) -> None:
    fp = canonical_hash(b'{"a": 1}')
    await claim_key(
        db_session, key="k11", endpoint=_ENDPOINT, fingerprint=fp, lock_ttl_seconds=_TTL
    )

    assert await load_key(db_session, key="k11", fingerprint=fp) is None


async def test_load_key_raises_reuse_on_fingerprint_mismatch(db_session: AsyncSession) -> None:
    fp1 = canonical_hash(b'{"a": 1}')
    fp2 = canonical_hash(b'{"a": 2}')
    await claim_key(
        db_session, key="k12", endpoint=_ENDPOINT, fingerprint=fp1, lock_ttl_seconds=_TTL
    )
    await complete_key(db_session, key="k12", response_status=201, response_body={})
    await db_session.commit()

    with pytest.raises(IdempotencyKeyReuse):
        await load_key(db_session, key="k12", fingerprint=fp2)
