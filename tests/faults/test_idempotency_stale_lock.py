"""SPEC.md §10: a stale lock reclaimed while the original request is still
in flight.

Deterministic, not a race: request A is gated inside `post_transaction` so
it holds a committed claim but hasn't reached the ledger write yet. We
backdate its lock past the TTL, let request B reclaim and complete, then
release A -- A's own INSERT trips `transactions_idempotency_key_key`,
raises `DuplicateTransaction`, and `IdempotentRequest.run` turns that into
a replay of B's response. This is the concrete proof of SPEC.md §6's "the
lock is an optimization; the unique constraint is the guarantee."
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.faults.conftest import backdate_lock, ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.fault, pytest.mark.timeout(30)]


async def _create_account(client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_stale_lock_reclaimed_while_original_in_flight(
    fault_client: AsyncClient,
    db_session: AsyncSession,
    concurrency_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
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

    gate = asyncio.Event()
    entered = asyncio.Event()
    gated_once = False
    from ledger.core.posting import post_transaction as real_post_transaction

    async def gated_post_transaction(*args: object, **kwargs: object) -> object:
        # Only request A -- the first caller -- gates. B's request must
        # reach the real post_transaction directly, or it would block on
        # the same gate and the test would deadlock on itself.
        nonlocal gated_once
        if not gated_once:
            gated_once = True
            entered.set()
            await gate.wait()
        return await real_post_transaction(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("ledger.api.routes.transactions.post_transaction", gated_post_transaction)

    a_task = asyncio.ensure_future(
        fault_client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    # A's claim must already be visible to a second connection -- this is
    # the direct evidence that the claim commits separately from execute.
    await backdate_lock(concurrency_engine, key, 31)

    b_response = await fault_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert b_response.status_code == 201
    assert "Idempotent-Replay" not in b_response.headers

    gate.set()
    a_response = await asyncio.wait_for(a_task, timeout=10)

    assert a_response.status_code == 201
    assert a_response.headers["Idempotent-Replay"] == "true"
    assert a_response.json()["id"] == b_response.json()["id"]

    assert await ledger_row_counts(db_session) == (1, 2, 1)
