import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.outbox import OutboxEvent

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_post_transaction_201_shape(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 1000, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 1000, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 201
    assert "Location" in response.headers
    body = response.json()
    assert body["status"] == "posted"
    assert body["reversal_of"] is None
    assert len(body["entries"]) == 2


async def test_post_transaction_updates_balances_and_entry_count(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 250, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 250, "currency": "USD"},
            ]
        },
    )
    response = await app_client.get(f"/v1/accounts/{cash}")
    assert response.json()["balance"] == 250
    assert response.json()["entry_count"] == 1


async def test_post_transaction_writes_outbox_event(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    txn_id = response.json()["id"]
    event = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "transaction.posted")
        )
    ).scalar_one()
    assert event.payload["transaction"]["id"] == txn_id


async def test_get_transaction_by_id(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    create = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    txn_id = create.json()["id"]
    response = await app_client.get(f"/v1/transactions/{txn_id}")
    assert response.status_code == 200
    assert response.json()["id"] == txn_id


async def test_get_transaction_404(app_client: AsyncClient) -> None:
    response = await app_client.get(f"/v1/transactions/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/transaction-not-found"


async def test_list_transactions_filter_by_external_ref(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ],
            "external_ref": "order-1",
        },
    )
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 20, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 20, "currency": "USD"},
            ],
            "external_ref": "order-2",
        },
    )
    response = await app_client.get("/v1/transactions", params={"external_ref": "order-1"})
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["external_ref"] == "order-1"


async def test_list_transactions_date_range_half_open(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    far_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = await app_client.get("/v1/transactions", params={"created_after": far_future})
    assert response.json()["items"] == []


async def test_multi_leg_transaction_via_api(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    tax = await _create_account(app_client, name="Tax", type="liability")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 110, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 100, "currency": "USD"},
                {"account_id": tax, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 201
    assert len(response.json()["entries"]) == 3
