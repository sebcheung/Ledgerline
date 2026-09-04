"""Outbox fan-out and webhook delivery (SPEC.md §8).

`Dispatcher` is not a request path -- it is not called from a route, so
unlike `ledger.core`/`ledger.reconciliation` it does not follow SPEC.md
§13's "accepts a session, never commits" rule. It owns an `AsyncEngine` and
is the transaction boundary for each of its own short steps (fan-out,
claim, record, sweep) -- see `ledger.reconciliation.runner
._record_failed_run` for the same "engine, not session" shape used for
writes outside a request's transaction.

Every timestamp comparison and write here uses the Postgres clock
(`func.now()`, `make_interval`), never `datetime.now()` in Python -- the
"DB clock, not the app clock" rule already established for idempotency
staleness and reconciliation windows (see docs/DECISIONS.md). This is also
what makes the dispatcher's steps testable without any injectable clock:
tests move timestamps into the past from an independent connection instead
of sleeping.
"""

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import DateTime, case, cast, func, literal, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.config import Settings, get_settings
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint
from ledger.observability.metrics import (
    WEBHOOK_DELIVERY_ATTEMPTS,
    WEBHOOK_DELIVERY_LATENCY,
    WEBHOOK_STALE_CLAIMS_SWEPT,
)
from ledger.webhooks.signing import build_envelope, serialize_envelope, signature_header

logger = logging.getLogger(__name__)

#: How much of a failed/erroring response body to keep. `last_error` is a
#: Text column; an unbounded exception message from a hostile or broken
#: endpoint is a cheap way to bloat the table.
MAX_LAST_ERROR_LENGTH = 2000

#: `last_error` for a row `sweep_stale_claims` dead-letters after
#: `webhook_max_reclaims` reclaims -- distinguishes this from a `last_error`
#: set by `_record` after an observed receiver outcome (see
#: `Dispatcher.sweep_stale_claims`).
RECLAIM_EXHAUSTED_ERROR = "reclaimed too many times without a successful delivery observation"


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    attempt_count: int
    url: str
    secret: str
    event_type: str
    payload: dict[str, Any]
    #: The outbox event's `created_at`, hydrated alongside `event_type`/
    #: `payload` by `claim_batch`'s existing `OutboxEvent` join -- lets
    #: `_record` observe `webhook_delivery_latency_seconds` from event
    #: creation to successful delivery without a second query.
    event_created_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivery_id: uuid.UUID
    status: WebhookDeliveryStatus
    response_code: int | None
    error: str | None
    retry_delay_seconds: float | None
    #: Only populated on the SUCCEEDED branch -- the sole outcome `_record`
    #: observes `webhook_delivery_latency_seconds` for.
    event_created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DispatchCycle:
    swept: int
    fanned_out: int
    claimed: int
    succeeded: int
    dead: int
    retried: int


def _describe(exc: Exception) -> str:
    """`httpx.ConnectError`/`ReadTimeout` frequently stringify to `''` (the
    underlying OS error carries no message on some platforms) -- fall back
    to the exception's class name so `last_error` is never an empty,
    useless string."""
    return str(exc) or type(exc).__name__


def compute_backoff(
    attempt: int, *, base_seconds: float, max_seconds: float, rng: random.Random
) -> float:
    """SPEC.md §8's full-jitter backoff: `random.uniform(0, base)`, not
    equal jitter (`base/2 + random.uniform(0, base/2)`) -- full jitter
    spreads retries across the whole window instead of clustering them
    around half of it, which is what actually prevents a thundering herd
    when many deliveries fail against one downed endpoint at once."""
    base = min(base_seconds * (2 ** (attempt - 1)), max_seconds)
    return rng.uniform(0, base)


class Dispatcher:
    def __init__(
        self,
        engine: AsyncEngine,
        client: httpx.AsyncClient,
        *,
        settings: Settings | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self._client = client
        self._settings = settings if settings is not None else get_settings()
        self._rng = rng if rng is not None else random.Random()

    async def fan_out(self) -> int:
        """Insert one `webhook_deliveries` row per active endpoint for
        every outbox event not yet fanned out, then mark those events
        fanned. `FOR UPDATE SKIP LOCKED` lets multiple dispatcher processes
        fan out concurrently without duplicating work; `ON CONFLICT DO
        NOTHING` on `(event_id, endpoint_id)` makes the whole step
        idempotent if the process crashes between the insert and the
        update (a re-run just re-fans the same claimed events and the
        conflict absorbs it).

        An endpoint registered after an event was already fanned out will
        never receive that event -- fan-out is a one-time snapshot of the
        active endpoint set at the time the event is drained, not a live
        subscription replayed against history.
        """
        async with self._engine.begin() as conn:
            claim_stmt = (
                select(OutboxEvent.id)
                .where(OutboxEvent.fanned_out_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .limit(self._settings.webhook_fanout_batch_size)
                .with_for_update(skip_locked=True)
            )
            claimed_ids = (await conn.execute(claim_stmt)).scalars().all()
            if not claimed_ids:
                return 0

            fan_out_select = (
                select(OutboxEvent.id, WebhookEndpoint.id, func.now())
                .select_from(OutboxEvent)
                .join(WebhookEndpoint, true())
                .where(OutboxEvent.id.in_(claimed_ids), WebhookEndpoint.active.is_(True))
            )
            insert_stmt = (
                pg_insert(WebhookDelivery)
                .from_select(["event_id", "endpoint_id", "next_attempt_at"], fan_out_select)
                .on_conflict_do_nothing(index_elements=["event_id", "endpoint_id"])
            )
            await conn.execute(insert_stmt)

            await conn.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_(claimed_ids))
                .values(fanned_out_at=func.now())
            )

        logger.info("webhook.fanned_out", extra={"event_count": len(claimed_ids)})
        return len(claimed_ids)

    async def claim_batch(self) -> list[ClaimedDelivery]:
        """A single `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
        LOCKED) RETURNING id` claim, rather than SPEC.md §8's two-statement
        SELECT-then-UPDATE pseudocode -- this closes the window between the
        two statements at no extra cost, and is served by the existing
        `ix_webhook_deliveries_status_next_attempt` index."""
        async with self._engine.begin() as conn:
            candidate_ids = (
                select(WebhookDelivery.id)
                .where(
                    WebhookDelivery.status == WebhookDeliveryStatus.PENDING,
                    WebhookDelivery.next_attempt_at <= func.now(),
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(self._settings.webhook_batch_size)
                .with_for_update(skip_locked=True)
            )
            claimed_ids = (
                (
                    await conn.execute(
                        update(WebhookDelivery)
                        .where(WebhookDelivery.id.in_(candidate_ids))
                        .values(status=WebhookDeliveryStatus.DELIVERING, claimed_at=func.now())
                        .returning(WebhookDelivery.id)
                    )
                )
                .scalars()
                .all()
            )
            if not claimed_ids:
                return []

            rows = (
                await conn.execute(
                    select(
                        WebhookDelivery.id,
                        WebhookDelivery.event_id,
                        WebhookDelivery.endpoint_id,
                        WebhookDelivery.attempt_count,
                        WebhookEndpoint.url,
                        WebhookEndpoint.secret,
                        OutboxEvent.event_type,
                        OutboxEvent.payload,
                        OutboxEvent.created_at,
                    )
                    .select_from(WebhookDelivery)
                    .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
                    .join(OutboxEvent, OutboxEvent.id == WebhookDelivery.event_id)
                    .where(WebhookDelivery.id.in_(claimed_ids))
                )
            ).all()

        claimed = [
            ClaimedDelivery(
                id=r.id,
                event_id=r.event_id,
                endpoint_id=r.endpoint_id,
                attempt_count=r.attempt_count,
                url=r.url,
                secret=r.secret,
                event_type=r.event_type,
                payload=r.payload,
                event_created_at=r.created_at,
            )
            for r in rows
        ]
        logger.info("webhook.claimed", extra={"count": len(claimed)})
        return claimed

    async def _deliver_one(
        self, row: ClaimedDelivery, semaphore: asyncio.Semaphore
    ) -> DeliveryOutcome:
        envelope = build_envelope(row.event_id, row.event_type, row.payload)
        body = serialize_envelope(envelope)
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-Ledgerline-Event-Id": str(row.event_id),
            "X-Ledgerline-Timestamp": str(timestamp),
            "X-Ledgerline-Signature": signature_header(row.secret, timestamp, body),
        }

        async with semaphore:
            try:
                response = await self._client.post(row.url, content=body, headers=headers)
            except asyncio.CancelledError:
                # A `BaseException` in Python 3.8+ -- not caught by any of
                # the `except Exception` clauses below. Raised when the
                # dispatcher's task is cancelled (graceful shutdown/SIGTERM)
                # while this delivery is in flight. The row was already
                # marked `delivering` by `claim_batch` before this coroutine
                # ever started, so `sweep_stale_claims` will reclaim it once
                # `claimed_at` goes stale -- there is nothing to record here.
                # Re-raise so `asyncio.gather` (without `return_exceptions=
                # True`) propagates the cancellation up through `deliver()`
                # and `run_once()`, which is correct shutdown behavior; a
                # cancelled task's return value would be discarded anyway.
                logger.warning("webhook.delivery_cancelled", extra={"delivery_id": str(row.id)})
                raise
            except httpx.TimeoutException as exc:
                return self._retry_outcome(row, response_code=None, error=_describe(exc))
            except httpx.TransportError as exc:
                return self._retry_outcome(row, response_code=None, error=_describe(exc))
            except Exception as exc:  # a bug here must not strand the row in `delivering`
                logger.error(
                    "webhook.delivery_unexpected_error",
                    extra={"delivery_id": str(row.id)},
                    exc_info=True,
                )
                return self._retry_outcome(row, response_code=None, error=_describe(exc))

        code = response.status_code
        if 200 <= code < 300:
            return DeliveryOutcome(
                delivery_id=row.id,
                status=WebhookDeliveryStatus.SUCCEEDED,
                response_code=code,
                error=None,
                retry_delay_seconds=None,
                event_created_at=row.event_created_at,
            )
        if code == 429 or code >= 500:
            return self._retry_outcome(row, response_code=code, error=f"HTTP {code}")
        # Any other 4xx (and any unexpected code, e.g. a redirect we did not
        # follow -- follow_redirects=False, since silently redirecting a
        # signed POST to an unverified host is not something to do): the
        # client will not fix itself by retrying.
        return DeliveryOutcome(
            delivery_id=row.id,
            status=WebhookDeliveryStatus.DEAD,
            response_code=code,
            error=f"HTTP {code}",
            retry_delay_seconds=None,
        )

    def _retry_outcome(
        self, row: ClaimedDelivery, *, response_code: int | None, error: str
    ) -> DeliveryOutcome:
        attempt = row.attempt_count + 1
        if attempt >= self._settings.webhook_max_attempts:
            return DeliveryOutcome(
                delivery_id=row.id,
                status=WebhookDeliveryStatus.DEAD,
                response_code=response_code,
                error=error[:MAX_LAST_ERROR_LENGTH],
                retry_delay_seconds=None,
            )
        delay = compute_backoff(
            attempt,
            base_seconds=self._settings.webhook_base_delay_seconds,
            max_seconds=self._settings.webhook_max_delay_seconds,
            rng=self._rng,
        )
        return DeliveryOutcome(
            delivery_id=row.id,
            status=WebhookDeliveryStatus.PENDING,
            response_code=response_code,
            error=error[:MAX_LAST_ERROR_LENGTH],
            retry_delay_seconds=delay,
        )

    async def deliver(self, claimed: Sequence[ClaimedDelivery]) -> list[DeliveryOutcome]:
        if not claimed:
            return []
        semaphore = asyncio.Semaphore(self._settings.webhook_concurrency)
        outcomes = await asyncio.gather(*(self._deliver_one(row, semaphore) for row in claimed))
        await self._record(outcomes)
        return list(outcomes)

    async def _record(self, outcomes: Sequence[DeliveryOutcome]) -> None:
        async with self._engine.begin() as conn:
            for outcome in outcomes:
                if outcome.status is WebhookDeliveryStatus.SUCCEEDED:
                    # func.now(), not datetime.now(): the module docstring's
                    # "DB clock, not the app clock" rule -- lets fault tests
                    # backdate OutboxEvent.created_at instead of sleeping to
                    # exercise a nonzero latency observation. Computed in the
                    # same UPDATE via RETURNING rather than a second query.
                    assert outcome.event_created_at is not None
                    latency_seconds = (
                        await conn.execute(
                            update(WebhookDelivery)
                            .where(WebhookDelivery.id == outcome.delivery_id)
                            .values(
                                status=WebhookDeliveryStatus.SUCCEEDED,
                                last_response_code=outcome.response_code,
                                claimed_at=None,
                            )
                            .returning(
                                func.extract(
                                    "epoch",
                                    func.now()
                                    # Explicit `timestamptz` type: an untyped
                                    # Python datetime literal defaults to
                                    # SQLAlchemy's timezone-naive DateTime,
                                    # which asyncpg then refuses to subtract
                                    # from `now()`'s timestamptz.
                                    - literal(
                                        outcome.event_created_at,
                                        type_=DateTime(timezone=True),
                                    ),
                                )
                            )
                        )
                    ).scalar_one()
                    WEBHOOK_DELIVERY_LATENCY.observe(float(latency_seconds))
                    WEBHOOK_DELIVERY_ATTEMPTS.labels(outcome="succeeded").inc()
                    logger.info(
                        "webhook.delivered", extra={"delivery_id": str(outcome.delivery_id)}
                    )
                elif outcome.status is WebhookDeliveryStatus.DEAD:
                    await conn.execute(
                        update(WebhookDelivery)
                        .where(WebhookDelivery.id == outcome.delivery_id)
                        .values(
                            status=WebhookDeliveryStatus.DEAD,
                            attempt_count=WebhookDelivery.attempt_count + 1,
                            last_error=outcome.error,
                            last_response_code=outcome.response_code,
                            claimed_at=None,
                        )
                    )
                    WEBHOOK_DELIVERY_ATTEMPTS.labels(outcome="dead").inc()
                    logger.warning(
                        "webhook.dead",
                        extra={
                            "delivery_id": str(outcome.delivery_id),
                            "response_code": outcome.response_code,
                        },
                    )
                else:
                    assert outcome.retry_delay_seconds is not None
                    await conn.execute(
                        update(WebhookDelivery)
                        .where(WebhookDelivery.id == outcome.delivery_id)
                        .values(
                            status=WebhookDeliveryStatus.PENDING,
                            attempt_count=WebhookDelivery.attempt_count + 1,
                            next_attempt_at=text(
                                "now() + make_interval(secs => :delay)"
                            ).bindparams(delay=outcome.retry_delay_seconds),
                            last_error=outcome.error,
                            last_response_code=outcome.response_code,
                            claimed_at=None,
                        )
                    )
                    WEBHOOK_DELIVERY_ATTEMPTS.labels(outcome="retried").inc()
                    logger.info(
                        "webhook.retry_scheduled",
                        extra={
                            "delivery_id": str(outcome.delivery_id),
                            "delay_seconds": outcome.retry_delay_seconds,
                        },
                    )

    async def sweep_stale_claims(self) -> int:
        """Reclaim rows a crashed worker left in `delivering`. Deliberately
        does not increment `attempt_count` -- a killed worker made no
        observation of the receiver, so charging it an attempt would
        shrink the retry budget for a fault that was ours, not the
        receiver's. This is what makes delivery at-least-once rather than
        at-most-once: the event ID header lets receivers dedupe (see
        README.md).

        `reclaim_count` is the separate budget that *does* get charged here:
        a worker that reliably crashes mid-POST (e.g. a bad deploy that
        segfaults right after the socket write) would otherwise never
        accumulate an `attempt_count` and would be redelivered forever. Once
        incrementing it would push it past `webhook_max_reclaims`, this
        dead-letters the row in the same UPDATE instead of resetting it to
        `pending` -- a CASE-based single statement rather than two, so the
        reclaim count and the status it gates can never observe each other's
        write half-applied."""
        exhausted = WebhookDelivery.reclaim_count + 1 >= self._settings.webhook_max_reclaims
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    update(WebhookDelivery)
                    .where(
                        WebhookDelivery.status == WebhookDeliveryStatus.DELIVERING,
                        WebhookDelivery.claimed_at
                        < text("now() - make_interval(secs => :stale)").bindparams(
                            stale=self._settings.webhook_stale_claim_seconds
                        ),
                    )
                    .values(
                        reclaim_count=WebhookDelivery.reclaim_count + 1,
                        # `cast(..., WebhookDelivery.status.type)`: asyncpg
                        # refuses to bind a `CASE` expression's inferred
                        # `text` result into a native-enum column without an
                        # explicit cast, unlike a plain `.values(status=...)`
                        # literal (SQLAlchemy already types that one from the
                        # target column).
                        status=cast(
                            case(
                                (exhausted, WebhookDeliveryStatus.DEAD),
                                else_=WebhookDeliveryStatus.PENDING,
                            ),
                            WebhookDelivery.status.type,
                        ),
                        last_error=case(
                            (exhausted, RECLAIM_EXHAUSTED_ERROR),
                            else_=WebhookDelivery.last_error,
                        ),
                        claimed_at=None,
                    )
                    .returning(WebhookDelivery.id, WebhookDelivery.status)
                )
            ).all()
        swept_ids = [r.id for r in rows]
        dead_ids = [r.id for r in rows if r.status is WebhookDeliveryStatus.DEAD]
        if swept_ids:
            WEBHOOK_STALE_CLAIMS_SWEPT.inc(len(swept_ids))
            logger.warning("webhook.stale_claim_swept", extra={"count": len(swept_ids)})
        if dead_ids:
            WEBHOOK_DELIVERY_ATTEMPTS.labels(outcome="dead").inc(len(dead_ids))
            logger.warning(
                "webhook.reclaim_exhausted",
                extra={
                    "count": len(dead_ids),
                    "delivery_ids": [str(i) for i in dead_ids],
                },
            )
        return len(swept_ids)

    async def run_once(self) -> DispatchCycle:
        swept = await self.sweep_stale_claims()
        fanned_out = await self.fan_out()
        claimed = await self.claim_batch()
        outcomes = await self.deliver(claimed)
        succeeded = sum(1 for o in outcomes if o.status is WebhookDeliveryStatus.SUCCEEDED)
        dead = sum(1 for o in outcomes if o.status is WebhookDeliveryStatus.DEAD)
        retried = sum(1 for o in outcomes if o.status is WebhookDeliveryStatus.PENDING)
        return DispatchCycle(
            swept=swept,
            fanned_out=fanned_out,
            claimed=len(claimed),
            succeeded=succeeded,
            dead=dead,
            retried=retried,
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=self._settings.webhook_poll_interval_seconds
                )
