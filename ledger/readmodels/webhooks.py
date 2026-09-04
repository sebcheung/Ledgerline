"""Webhook delivery queue read model for the dashboard's webhook panel.

Every retry countdown and staleness flag is computed with the *Postgres*
clock inside the query, never `datetime.now()` in Python -- the "DB clock,
not the app clock" rule `ledger.webhooks.dispatcher` already establishes for
timestamp comparisons. This also makes both values assertable against an
exact expected integer in a test, after moving a row's timestamp with a
`backdate_next_attempt`-style helper, instead of a fuzzy range.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint


@dataclass(frozen=True, slots=True)
class QueueSummary:
    counts: dict[WebhookDeliveryStatus, int]
    dlq_depth: int
    server_time: datetime


@dataclass(frozen=True, slots=True)
class DeliveryQueueRow:
    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    endpoint_url: str
    event_type: str
    status: WebhookDeliveryStatus
    attempt_count: int
    reclaim_count: int
    next_attempt_at: datetime
    last_error: str | None
    last_response_code: int | None
    claimed_at: datetime | None
    created_at: datetime
    #: Negative or zero means already due. `None` for statuses with no
    #: pending retry (`succeeded`, or a `dead` row past its attempt budget).
    seconds_until_retry: int | None
    claim_is_stale: bool


async def load_queue_summary(session: AsyncSession) -> QueueSummary:
    """One grouped query gives every `WebhookDeliveryStatus` bucket count
    and the server clock; absent buckets are filled in as 0 below so all
    four statuses always render. `dlq_depth` (SPEC.md §9) is the `dead`
    bucket."""
    stmt = select(
        WebhookDelivery.status, func.count().label("n"), func.now().label("server_time")
    ).group_by(WebhookDelivery.status)
    rows = (await session.execute(stmt)).all()

    counts: dict[WebhookDeliveryStatus, int] = dict.fromkeys(WebhookDeliveryStatus, 0)
    server_time = (await session.execute(select(func.now()))).scalar_one()
    for r in rows:
        counts[r.status] = r.n
        server_time = r.server_time

    return QueueSummary(
        counts=counts,
        dlq_depth=counts[WebhookDeliveryStatus.DEAD],
        server_time=server_time,
    )


async def load_delivery_queue(session: AsyncSession, limit: int) -> list[DeliveryQueueRow]:
    """Every delivery not yet `succeeded`, joined for endpoint/event context,
    ordered so the operationally interesting rows (dead, then delivering,
    then pending) sort first."""
    settings = get_settings()
    stale_cutoff = text("now() - make_interval(secs => :stale)").bindparams(
        stale=settings.webhook_stale_claim_seconds
    )
    seconds_until_retry = cast(
        func.ceil(func.extract("epoch", WebhookDelivery.next_attempt_at - func.now())), Integer
    ).label("seconds_until_retry")
    claim_is_stale = (WebhookDelivery.claimed_at < stale_cutoff).label("claim_is_stale")

    status_order = {
        WebhookDeliveryStatus.DEAD: 0,
        WebhookDeliveryStatus.DELIVERING: 1,
        WebhookDeliveryStatus.PENDING: 2,
    }
    order_expr = case(status_order, value=WebhookDelivery.status, else_=3)

    stmt = (
        select(
            WebhookDelivery.id,
            WebhookDelivery.event_id,
            WebhookDelivery.endpoint_id,
            WebhookEndpoint.url.label("endpoint_url"),
            OutboxEvent.event_type,
            WebhookDelivery.status,
            WebhookDelivery.attempt_count,
            WebhookDelivery.reclaim_count,
            WebhookDelivery.next_attempt_at,
            WebhookDelivery.last_error,
            WebhookDelivery.last_response_code,
            WebhookDelivery.claimed_at,
            WebhookDelivery.created_at,
            seconds_until_retry,
            claim_is_stale,
        )
        .select_from(WebhookDelivery)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .join(OutboxEvent, OutboxEvent.id == WebhookDelivery.event_id)
        .where(WebhookDelivery.status != WebhookDeliveryStatus.SUCCEEDED)
        .order_by(order_expr, WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        DeliveryQueueRow(
            id=r.id,
            event_id=r.event_id,
            endpoint_id=r.endpoint_id,
            endpoint_url=r.endpoint_url,
            event_type=r.event_type,
            status=r.status,
            attempt_count=r.attempt_count,
            reclaim_count=r.reclaim_count,
            next_attempt_at=r.next_attempt_at,
            last_error=r.last_error,
            last_response_code=r.last_response_code,
            claimed_at=r.claimed_at,
            created_at=r.created_at,
            seconds_until_retry=r.seconds_until_retry,
            claim_is_stale=bool(r.claim_is_stale),
        )
        for r in rows
    ]
