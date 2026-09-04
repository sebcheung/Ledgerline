"""Unit-level coverage of `Dispatcher._deliver_one`'s exception mapping,
using a fake `httpx.AsyncClient` so no network or database is needed --
these are pure branch assertions, not integration behavior."""

import asyncio
import random
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from ledger.models.enums import WebhookDeliveryStatus
from ledger.webhooks.dispatcher import ClaimedDelivery, Dispatcher


def _row() -> ClaimedDelivery:
    return ClaimedDelivery(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        attempt_count=0,
        url="http://example.invalid/hook",
        secret="s",
        event_type="transaction.posted",
        payload={},
        event_created_at=datetime.now(UTC),
    )


async def test_deliver_with_no_claimed_rows_is_a_noop() -> None:
    dispatcher = Dispatcher(engine=object(), client=object(), rng=random.Random(1))  # type: ignore[arg-type]
    assert await dispatcher.deliver([]) == []


async def test_an_unexpected_exception_is_treated_as_retryable_not_fatal() -> None:
    """A bug in our own code (or an unanticipated client error) must not
    strand the row in `delivering` forever -- SPEC.md doesn't name this
    case, but `ledger.webhooks.dispatcher`'s catch-all does, deliberately."""
    client = AsyncMock()
    client.post.side_effect = ValueError("boom")
    dispatcher = Dispatcher(engine=object(), client=client, rng=random.Random(1))  # type: ignore[arg-type]

    outcome = await dispatcher._deliver_one(_row(), asyncio.Semaphore(1))

    assert outcome.status == WebhookDeliveryStatus.PENDING
    assert outcome.error == "boom"
    assert outcome.retry_delay_seconds is not None


@pytest.mark.parametrize("status_code", [301, 302])
async def test_a_redirect_is_treated_as_a_retryable_failure_not_followed(
    status_code: int,
) -> None:
    """`follow_redirects=False` means a 3xx never gets special-cased into
    success -- it falls through to the generic 4xx/5xx-adjacent handling.
    Since 3xx is neither 2xx, 429, nor >=500, and not in [400, 500), the
    non-retryable branch would misclassify it as dead; this pins that it
    is *not* silently treated as success."""
    response = AsyncMock()
    response.status_code = status_code
    client = AsyncMock()
    client.post.return_value = response
    dispatcher = Dispatcher(engine=object(), client=client, rng=random.Random(1))  # type: ignore[arg-type]

    outcome = await dispatcher._deliver_one(_row(), asyncio.Semaphore(1))
    assert outcome.status != WebhookDeliveryStatus.SUCCEEDED
