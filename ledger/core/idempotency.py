"""Idempotency protocol (SPEC.md §6).

This module must never import from `ledger.api` or `fastapi` -- same rule
`ledger.core.errors` states for the api -> core dependency arrow, so
`ledger.reconciliation` (Phase 4) can reuse this protocol for
`POST /v1/reconciliation/runs` without pulling in the web framework.

## The claim commits; everything else obeys the caller-commits rule

Every other function in `ledger.core` (see `posting.py`) never commits --
the caller owns the transaction boundary. `claim_key` is the one exception:
it commits internally. This is not a style inconsistency, it is a
correctness requirement. Postgres's `INSERT ... ON CONFLICT DO NOTHING`
uses speculative insertion, and when it conflicts with a row inserted by an
*uncommitted* transaction it blocks on that transaction's outcome. If the
claim shared a transaction with the (potentially slow) execute step, every
concurrent duplicate request would queue behind the winner's entire
posting instead of observing the `in_progress` row and returning a fast
409 -- SPEC.md §10's "20 concurrent, 1 execution, 19 replays or 409s" would
degenerate into 20 executions run one at a time, proving nothing.

This does *not* contradict SPEC.md §6's "the single commit at the end is
the crux". That statement is about which two things share a commit: the
ledger write and the key's `status='completed'` UPDATE. The claim is a
separate, earlier commit -- see `ledger.api.idempotent.IdempotentRequest.run`
for where the crux commit actually happens.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import delete, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import Label

from ledger.core.errors import DuplicateTransaction as IdempotencyConflict
from ledger.core.errors import (
    IdempotencyKeyReuse,
    IdempotencyKeyScopeConflict,
    IdempotencyStateError,
    InvalidRequestBody,
)
from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey

logger = logging.getLogger(__name__)

#: SPEC.md §6: reclaiming a stale lock races with the possibility that the
#: original request is still mid-flight. The claim row can vanish between
#: our ON CONFLICT DO NOTHING and our SELECT ... FOR UPDATE only because
#: `release_key` (a divergence from §6, which has no DELETE) can run
#: concurrently; a bounded retry absorbs that narrow window.
_MAX_CLAIM_ATTEMPTS = 3

__all__ = [
    "Claim",
    "ClaimOutcome",
    "IdempotencyConflict",
    "StoredResponse",
    "canonical_hash",
    "canonical_json",
    "claim_key",
    "complete_key",
    "load_key",
    "release_key",
]


def canonical_json(value: Any) -> str:
    """Canonicalize a JSON-compatible value: recursively drop `None`
    *object* members (SPEC.md §6 does not say array elements, and dropping
    those would shift indices and wrongly equate e.g. `[1, null, 2]` with
    `[1, 2]`), then serialize with sorted keys and no whitespace."""
    return json.dumps(
        _drop_null_members(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _drop_null_members(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_null_members(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_null_members(v) for v in value]
    return value


def _parse_body(body: bytes | None) -> Any:
    if body is None or not body.strip():
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidRequestBody("request body is not valid JSON") from exc


def canonical_hash(body: bytes | None, path_params: Mapping[str, Any] | None = None) -> str:
    """SHA-256 hex digest fingerprinting a request for idempotency
    comparison.

    Deliberately hashes `{"body": ..., "path": ...}` rather than the body
    alone, which is what SPEC.md §6's `canonical_hash(request.body)`
    literally says. `POST /v1/transactions/{id}/reverse` has no body at
    all, so a body-only fingerprint would be identical for every reversal
    -- a stale lock reclaimed for one transaction could silently replay (or
    worse, re-execute) against a different one. Folding path params in
    keeps `(key, endpoint)` scoping (SPEC.md §6) meaningful for
    empty-bodied routes without inventing a new mechanism.
    """
    document = {
        "body": _parse_body(body),
        "path": {k: str(v) for k, v in (path_params or {}).items()},
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


class ClaimOutcome(StrEnum):
    EXECUTE = "execute"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class Claim:
    outcome: ClaimOutcome
    key: str
    endpoint: str
    fingerprint: str
    #: The `locked_at` this claim (or reclaim) wrote. `None` on REPLAY --
    #: nothing was claimed, there is nothing to fence a later release on.
    locked_at: datetime | None
    replay: StoredResponse | None = None


async def claim_key(
    session: AsyncSession,
    *,
    key: str,
    endpoint: str,
    fingerprint: str,
    lock_ttl_seconds: int,
) -> Claim:
    """SPEC.md §6's claim algorithm, verbatim except where noted above and
    in the DuplicateTransaction/IdempotencyConflict split.

    On EXECUTE or a stale reclaim, commits before returning -- see the
    module docstring. On every other path (REPLAY, or a raised error) the
    claim's own transaction is rolled back before returning/raising, so the
    caller never inherits an open transaction from this function.
    """
    for _attempt in range(_MAX_CLAIM_ATTEMPTS):
        insert_stmt = (
            pg_insert(IdempotencyKey)
            .values(
                key=key,
                endpoint=endpoint,
                request_fingerprint=fingerprint,
                status=IdempotencyStatus.IN_PROGRESS,
                locked_at=text("now()"),
            )
            .on_conflict_do_nothing(index_elements=[IdempotencyKey.key])
            .returning(IdempotencyKey.locked_at)
        )
        inserted = (await session.execute(insert_stmt)).one_or_none()
        if inserted is not None:
            await session.commit()
            logger.info("idempotency.claimed", extra={"idempotency_key": key, "endpoint": endpoint})
            return Claim(
                outcome=ClaimOutcome.EXECUTE,
                key=key,
                endpoint=endpoint,
                fingerprint=fingerprint,
                locked_at=inserted.locked_at,
            )

        # lock_ttl_seconds is a trusted int from ledger.config.Settings, never
        # user input -- safe to inline rather than fight SQLAlchemy's lack of
        # bind-parameter support inside a labeled literal_column expression.
        is_stale_expr: Label[bool] = literal_column(
            f"now() - locked_at > make_interval(secs => {int(lock_ttl_seconds)})"
        ).label("is_stale")
        row = (
            await session.execute(
                select(
                    IdempotencyKey.endpoint,
                    IdempotencyKey.request_fingerprint,
                    IdempotencyKey.status,
                    IdempotencyKey.response_status,
                    IdempotencyKey.response_body,
                    IdempotencyKey.locked_at,
                    is_stale_expr,
                )
                .where(IdempotencyKey.key == key)
                .with_for_update()
            )
        ).one_or_none()

        if row is None:
            # The claim we lost to was released (see release_key) between
            # our INSERT and this SELECT. Retry the whole claim -- from
            # this request's point of view, the key is simply unclaimed.
            await session.rollback()
            continue

        if row.endpoint != endpoint:
            await session.rollback()
            raise IdempotencyKeyScopeConflict(
                f"idempotency key {key!r} was already claimed for a different endpoint",
                idempotency_key=key,
                endpoint=endpoint,
                claimed_endpoint=row.endpoint,
            )

        if row.status == IdempotencyStatus.COMPLETED:
            await session.rollback()
            if row.request_fingerprint == fingerprint:
                logger.info("idempotency.replayed", extra={"idempotency_key": key})
                return Claim(
                    outcome=ClaimOutcome.REPLAY,
                    key=key,
                    endpoint=endpoint,
                    fingerprint=fingerprint,
                    locked_at=None,
                    replay=StoredResponse(status=row.response_status, body=row.response_body),
                )
            raise IdempotencyKeyReuse(
                f"idempotency key {key!r} was reused with a different request body",
                idempotency_key=key,
            )

        # status == IN_PROGRESS
        if not row.is_stale:
            await session.rollback()
            logger.info("idempotency.conflict", extra={"idempotency_key": key})
            raise IdempotencyConflict(
                f"a request with idempotency key {key!r} is already in progress",
                idempotency_key=key,
            )

        if row.request_fingerprint != fingerprint:
            await session.rollback()
            raise IdempotencyKeyReuse(
                f"idempotency key {key!r} was reused with a different request body"
                " while its stale lock was pending reclamation",
                idempotency_key=key,
            )

        reclaim_stmt = (
            update(IdempotencyKey)
            .where(IdempotencyKey.key == key)
            .values(locked_at=text("now()"))
            .returning(IdempotencyKey.locked_at)
        )
        reclaimed = (await session.execute(reclaim_stmt)).one()
        await session.commit()
        logger.info("idempotency.reclaimed", extra={"idempotency_key": key, "endpoint": endpoint})
        return Claim(
            outcome=ClaimOutcome.EXECUTE,
            key=key,
            endpoint=endpoint,
            fingerprint=fingerprint,
            locked_at=reclaimed.locked_at,
        )

    raise IdempotencyConflict(
        f"could not claim idempotency key {key!r} after {_MAX_CLAIM_ATTEMPTS} attempts",
        idempotency_key=key,
    )


async def complete_key(
    session: AsyncSession, *, key: str, response_status: int, response_body: dict[str, Any]
) -> None:
    """Mark a claimed key completed and store its response envelope.

    Never commits -- the caller (`IdempotentRequest.run`) commits this in
    the same transaction as the ledger write it is completing, which is
    the crux SPEC.md §6 describes.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(IdempotencyKey)
            .where(
                IdempotencyKey.key == key, IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS
            )
            .values(
                status=IdempotencyStatus.COMPLETED,
                response_status=response_status,
                response_body=response_body,
            )
        ),
    )
    if result.rowcount != 1:
        raise IdempotencyStateError(
            f"expected exactly one in-progress idempotency key {key!r} to complete,"
            f" found {result.rowcount}",
            idempotency_key=key,
        )


async def release_key(session: AsyncSession, *, key: str, locked_at: datetime) -> None:
    """Delete a claim that will never be completed, because `execute_fn`
    raised something other than `DuplicateTransaction` -- SPEC.md §6 does
    not consider this case. The alternative -- leaving the key
    `in_progress` -- would force a client correcting e.g. an
    `InsufficientFunds` typo to wait out the full TTL before retrying a
    request that had no ledger effect.

    Fenced on `locked_at`: a slow original failing late must not delete a
    reclaimer's live claim out from under it. `complete_key` has no such
    fence, because whichever request actually wins the
    `transactions.idempotency_key` unique constraint is by construction
    the only one that can reach it.

    Commits -- like `claim_key`, this is a visibility requirement (a
    retried request must see the key gone, not a lingering in-progress
    row), not caller policy.
    """
    await session.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS,
            IdempotencyKey.locked_at == locked_at,
        )
    )
    await session.commit()
    logger.info("idempotency.released", extra={"idempotency_key": key})


async def load_key(session: AsyncSession, *, key: str, fingerprint: str) -> StoredResponse | None:
    """Re-read a key's stored response after `DuplicateTransaction` fires
    (SPEC.md §6's except branch: "if completed: replay row's stored
    response").

    Returns `None` if the key is not (yet) completed -- the caller reports
    409 in that case, per §6. Raises `IdempotencyKeyReuse` if the completed
    row's fingerprint does not match `fingerprint`: §6 replays
    unconditionally here, but the only way to reach a completed row with a
    different fingerprint than the request that just lost the unique-
    constraint race is a key written outside this protocol (e.g. Phase 4's
    resolver reusing a key). Handing back a stored response for a
    different request would be a worse failure mode than a 422.
    """
    row = (
        await session.execute(
            select(
                IdempotencyKey.status,
                IdempotencyKey.request_fingerprint,
                IdempotencyKey.response_status,
                IdempotencyKey.response_body,
            ).where(IdempotencyKey.key == key)
        )
    ).one_or_none()
    if row is None or row.status != IdempotencyStatus.COMPLETED:
        return None
    if row.request_fingerprint != fingerprint:
        raise IdempotencyKeyReuse(
            f"idempotency key {key!r} completed under a different request body"
            " than the one that just conflicted",
            idempotency_key=key,
        )
    return StoredResponse(status=row.response_status, body=row.response_body)
