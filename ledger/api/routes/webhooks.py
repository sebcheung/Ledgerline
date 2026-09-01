"""Webhook endpoint and delivery management (SPEC.md §8, §9).

Not on SPEC.md §6's idempotent-endpoint list. `create_endpoint` needs no
idempotency guard beyond ordinary POST semantics -- a duplicate call just
creates a second endpoint with a second secret, which is a config mistake
for the caller to notice and clean up, not a ledger-money hazard.
`retry_delivery` posts no new money either, but it does mutate a specific
row exactly once, so it uses the same `SELECT ... FOR UPDATE` plus
compare-and-swap `UPDATE ... WHERE status = 'dead'` shape as
`resolve_finding` (ledger/api/routes/reconciliation.py).
"""

import logging
import secrets
import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute

from ledger.api.deps import SessionDep
from ledger.core.errors import DeliveryNotRetryable, WebhookDeliveryNotFound
from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint
from ledger.schemas.pagination import Page, decode_cursor, encode_cursor
from ledger.schemas.webhooks import (
    WebhookDeliveryListQuery,
    WebhookDeliveryRead,
    WebhookEndpointCreate,
    WebhookEndpointCreated,
    WebhookEndpointListQuery,
    WebhookEndpointRead,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ENDPOINT_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    WebhookEndpoint.id,
    WebhookEndpoint.url,
    WebhookEndpoint.active,
    WebhookEndpoint.created_at,
)

_DELIVERY_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    WebhookDelivery.id,
    WebhookDelivery.event_id,
    WebhookDelivery.endpoint_id,
    WebhookDelivery.status,
    WebhookDelivery.attempt_count,
    WebhookDelivery.next_attempt_at,
    WebhookDelivery.last_error,
    WebhookDelivery.last_response_code,
    WebhookDelivery.claimed_at,
    WebhookDelivery.created_at,
)


@router.post("/webhooks/endpoints", response_model=WebhookEndpointCreated, status_code=201)
async def create_endpoint(
    payload: WebhookEndpointCreate, session: SessionDep
) -> WebhookEndpointCreated:
    # Server-generated, never client-supplied -- a client-chosen secret
    # invites a weak one, and there is no rotation endpoint that would make
    # accepting one useful anyway. Returned exactly once, in this response.
    secret = secrets.token_urlsafe(32)
    row = (
        await session.execute(
            insert(WebhookEndpoint)
            .values(url=str(payload.url), secret=secret, active=payload.active)
            .returning(*_ENDPOINT_COLUMNS)
        )
    ).one()
    await session.commit()
    return WebhookEndpointCreated(
        id=row.id, url=row.url, active=row.active, created_at=row.created_at, secret=secret
    )


@router.get("/webhooks/endpoints", response_model=Page[WebhookEndpointRead])
async def list_endpoints(
    session: SessionDep,
    query: Annotated[WebhookEndpointListQuery, Query()],
) -> Page[WebhookEndpointRead]:
    stmt = select(*_ENDPOINT_COLUMNS)
    if query.active is not None:
        stmt = stmt.where(WebhookEndpoint.active == query.active)
    if query.cursor is not None:
        c = decode_cursor(query.cursor)
        stmt = stmt.where(
            (WebhookEndpoint.created_at < c.created_at)
            | ((WebhookEndpoint.created_at == c.created_at) & (WebhookEndpoint.id < c.id))
        )
    stmt = stmt.order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc()).limit(
        query.limit + 1
    )

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]
    items = [WebhookEndpointRead.model_validate(r) for r in rows]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return Page[WebhookEndpointRead](items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/webhooks/deliveries", response_model=Page[WebhookDeliveryRead])
async def list_deliveries(
    session: SessionDep,
    query: Annotated[WebhookDeliveryListQuery, Query()],
) -> Page[WebhookDeliveryRead]:
    stmt = select(*_DELIVERY_COLUMNS)
    if query.status is not None:
        stmt = stmt.where(WebhookDelivery.status == query.status)
    if query.endpoint_id is not None:
        stmt = stmt.where(WebhookDelivery.endpoint_id == query.endpoint_id)
    if query.cursor is not None:
        c = decode_cursor(query.cursor)
        stmt = stmt.where(
            (WebhookDelivery.created_at < c.created_at)
            | ((WebhookDelivery.created_at == c.created_at) & (WebhookDelivery.id < c.id))
        )
    stmt = stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(
        query.limit + 1
    )

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > query.limit
    rows = rows[: query.limit]
    items = [WebhookDeliveryRead.model_validate(r) for r in rows]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return Page[WebhookDeliveryRead](items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("/webhooks/deliveries/{delivery_id}/retry", response_model=WebhookDeliveryRead)
async def retry_delivery(delivery_id: uuid.UUID, session: SessionDep) -> WebhookDeliveryRead:
    row = (
        await session.execute(
            select(WebhookDelivery.id, WebhookDelivery.status)
            .where(WebhookDelivery.id == delivery_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise WebhookDeliveryNotFound(f"webhook delivery {delivery_id} not found")
    if row.status != WebhookDeliveryStatus.DEAD:
        raise DeliveryNotRetryable(
            f"delivery {delivery_id} is {row.status.value}, not dead", status=row.status.value
        )

    # attempt_count resets to 0: a manual replay is a fresh retry budget,
    # not a continuation of the one that already exhausted itself.
    # last_error / last_response_code are preserved for forensics.
    cas_result = cast(
        CursorResult[Any],
        await session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.status == WebhookDeliveryStatus.DEAD,
            )
            .values(
                status=WebhookDeliveryStatus.PENDING,
                next_attempt_at=func.now(),
                attempt_count=0,
                claimed_at=None,
            )
        ),
    )
    if cas_result.rowcount != 1:
        raise DeliveryNotRetryable(f"delivery {delivery_id} was changed concurrently")

    await session.commit()

    final_row = (
        await session.execute(select(*_DELIVERY_COLUMNS).where(WebhookDelivery.id == delivery_id))
    ).one()
    logger.info("webhook.manual_retry", extra={"delivery_id": str(delivery_id)})
    return WebhookDeliveryRead.model_validate(final_row)
