"""SPEC.md §10-shaped fault test for rate limiting (Phase 7, SPEC.md §9):
load shedding under a token bucket must never half-post a transaction --
every 429 corresponds to a request whose ledger effect never happened.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.ratelimit import RateLimiter
from tests.faults.conftest import ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.slow, pytest.mark.timeout(60)]

CONCURRENCY = 250
BURST = 50


async def test_shedding_under_load_never_half_posts(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A tight, real-clock burst -- small enough that 250 concurrent
    # requests are guaranteed to exceed it (unlike the real 100/200
    # default, which this many fast local requests might not exhaust),
    # large enough that at least a few requests are admitted.
    fault_client._transport.app.state.rate_limiter = RateLimiter(  # type: ignore[attr-defined]
        rate_per_second=1.0, burst=float(BURST)
    )

    create = await fault_client.post(
        "/v1/accounts",
        json={"name": "Cash", "type": "asset", "currency": "USD", "allow_negative": True},
    )
    cash = create.json()["id"]
    create = await fault_client.post(
        "/v1/accounts", json={"name": "Revenue", "type": "revenue", "currency": "USD"}
    )
    revenue = create.json()["id"]

    async def attempt() -> int:
        response = await fault_client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"},
                    {
                        "account_id": revenue,
                        "direction": "credit",
                        "amount": 100,
                        "currency": "USD",
                    },
                ]
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        return response.status_code

    statuses = await asyncio.wait_for(
        asyncio.gather(*[attempt() for _ in range(CONCURRENCY)]), timeout=45
    )

    successes = [s for s in statuses if s == 201]
    shed = [s for s in statuses if s == 429]
    assert len(successes) + len(shed) == CONCURRENCY
    assert 0 < len(successes) <= BURST
    assert len(shed) > 0

    assert await ledger_row_counts(db_session) == (
        len(successes),
        len(successes) * 2,
        len(successes),
    )
