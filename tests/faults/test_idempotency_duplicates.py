"""SPEC.md §10: sequential replay of a duplicate request."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.faults.conftest import ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.fault]


async def _create_account(client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def _transfer_body(cash: str, revenue: str, amount: int = 1000) -> dict[str, object]:
    return {
        "entries": [
            {"account_id": cash, "direction": "debit", "amount": amount, "currency": "USD"},
            {"account_id": revenue, "direction": "credit", "amount": amount, "currency": "USD"},
        ]
    }


async def test_same_key_and_body_replays_the_original_response(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    key = str(uuid.uuid4())

    first = await fault_client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    assert "Idempotent-Replay" not in first.headers

    second = await fault_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert second.status_code == 201
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.headers["Location"] == first.headers["Location"]

    assert await ledger_row_counts(db_session) == (1, 2, 1)


async def test_same_key_and_reverse_replays_the_original_response(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    posted = (await fault_client.post("/v1/transactions", json=body)).json()

    key = str(uuid.uuid4())
    url = f"/v1/transactions/{posted['id']}/reverse"
    first = await fault_client.post(url, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    assert "Idempotent-Replay" not in first.headers

    second = await fault_client.post(url, headers={"Idempotency-Key": key})
    assert second.status_code == 201
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]

    # One original + one reversal transaction, four entries. Three outbox
    # events: the original's transaction.posted, plus reverse_transaction's
    # own transaction.posted (for the mirrored txn) and transaction.reversed
    # (see test_reverse_writes_outbox_event for the same 2:1 split).
    assert await ledger_row_counts(db_session) == (2, 4, 3)


async def test_no_header_creates_two_transactions_and_writes_no_key_row(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)

    r1 = await fault_client.post("/v1/transactions", json=body)
    r2 = await fault_client.post("/v1/transactions", json=body)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]

    txns, _entries, _outbox = await ledger_row_counts(db_session)
    assert txns == 2
    key_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM idempotency_keys"))
    ).scalar_one()
    assert key_count == 0
