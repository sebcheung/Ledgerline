"""Minimal outbox writer.

The event row is written inside the *same* DB transaction as the ledger
write it describes (SPEC.md §5 step 9 / §8 "Why an outbox"): if the ledger
commits, the event exists, with no dual-write gap. That guarantee is the
entire point of the outbox pattern and is independent of when a dispatcher
exists to drain it -- so Phase 2 writes these rows even though the
dispatcher itself (fan-out to `webhook_deliveries`, HTTP delivery, retries)
is Phase 5. See docs/DECISIONS.md.
"""

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.outbox import OutboxEvent

if TYPE_CHECKING:
    from ledger.core.posting import PostedTransaction

EVENT_TRANSACTION_POSTED = "transaction.posted"
EVENT_TRANSACTION_REVERSED = "transaction.reversed"


async def emit_event(
    session: AsyncSession, event_type: str, payload: Mapping[str, Any]
) -> uuid.UUID:
    """Append an outbox_events row inside the caller's transaction. Never
    commits -- the caller (post_transaction/reverse_transaction) owns the
    transaction boundary, per SPEC.md §13."""
    stmt = (
        insert(OutboxEvent)
        .values(event_type=event_type, payload=dict(payload))
        .returning(OutboxEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


def transaction_event_payload(txn: "PostedTransaction") -> dict[str, Any]:
    """The JSON-native wire shape for a `transaction.posted`/
    `transaction.reversed` event. asyncpg serializes JSONB via plain
    `json.dumps` with no custom default, so every value here must already
    be a JSON-native type (UUID -> str, datetime -> isoformat, enum ->
    .value).

    `idempotency_key` is deliberately omitted -- it is a client-supplied
    request identifier, and there is no reason to hand it to a third-party
    webhook endpoint.
    """
    return {
        "transaction": {
            "id": str(txn.id),
            "status": txn.status.value,
            "source": txn.source.value,
            "external_ref": txn.external_ref,
            "description": txn.description,
            "reversal_of": str(txn.reversal_of) if txn.reversal_of else None,
            "currency": txn.currency,
            "created_at": txn.created_at.isoformat(),
        },
        "entries": [
            {
                "id": str(e.id),
                "account_id": str(e.account_id),
                "direction": e.direction.value,
                "amount": e.amount,
                "currency": e.currency,
                "created_at": e.created_at.isoformat(),
            }
            for e in txn.entries
        ],
    }
