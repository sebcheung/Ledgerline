"""SPEC.md §10: same key + body, 20 concurrent requests -- one execution,
the rest replays or conflicts."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.faults.conftest import ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.slow, pytest.mark.timeout(60)]

CONCURRENCY = 20


async def _create_account(client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_twenty_concurrent_duplicates_one_execution(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = {
        "entries": [
            {"account_id": cash, "direction": "debit", "amount": 1000, "currency": "USD"},
            {"account_id": revenue, "direction": "credit", "amount": 1000, "currency": "USD"},
        ]
    }
    key = str(uuid.uuid4())

    async def attempt() -> tuple[int, bool]:
        response = await fault_client.post(
            "/v1/transactions", json=body, headers={"Idempotency-Key": key}
        )
        return response.status_code, response.headers.get("Idempotent-Replay") == "true"

    results = await asyncio.wait_for(
        asyncio.gather(*[attempt() for _ in range(CONCURRENCY)]), timeout=45
    )

    originals = [r for r in results if r == (201, False)]
    replays = [r for r in results if r == (201, True)]
    conflicts = [r for r in results if r[0] == 409]

    # Exactly one execution. Every other response is a replay or a fast
    # 409 -- which one is genuinely timing-dependent, so only the total is
    # asserted, not the split (asserting the split is how this test would
    # go flaky).
    assert len(originals) == 1
    assert len(replays) + len(conflicts) == CONCURRENCY - 1
    assert len(originals) + len(replays) + len(conflicts) == CONCURRENCY

    assert await ledger_row_counts(db_session) == (1, 2, 1)

    # A second wave, after the key has settled to completed, must be all
    # replays -- this is the state the first wave's conflicts converge to.
    second_wave = await asyncio.wait_for(
        asyncio.gather(*[attempt() for _ in range(CONCURRENCY)]), timeout=45
    )
    assert all(status == 201 and replay for status, replay in second_wave)
    assert await ledger_row_counts(db_session) == (1, 2, 1)
