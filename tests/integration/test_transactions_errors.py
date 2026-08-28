import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.entries import Entry
from ledger.models.outbox import OutboxEvent
from ledger.models.transactions import Transaction

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def _row_counts(session: AsyncSession) -> tuple[int, int, int]:
    txn_count = (await session.execute(select(Transaction.id))).scalars().all()
    entry_count = (await session.execute(select(Entry.id))).scalars().all()
    event_count = (await session.execute(select(OutboxEvent.id))).scalars().all()
    return len(txn_count), len(entry_count), len(event_count)


def _assert_problem(response: Response, status: int, type_: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == type_
    assert "title" in body
    assert "detail" in body
    assert "Traceback" not in body["detail"]
    assert "asyncpg" not in body["detail"]
    assert "psycopg" not in body["detail"]


async def test_unbalanced_transaction(app_client: AsyncClient, db_session: AsyncSession) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    before = await _row_counts(db_session)

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 99, "currency": "USD"},
            ]
        },
    )
    _assert_problem(response, 422, "/errors/unbalanced-transaction")
    assert await _row_counts(db_session) == before


async def test_insufficient_funds(app_client: AsyncClient, db_session: AsyncSession) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset", allow_negative=False)
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    before = await _row_counts(db_session)

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": revenue, "direction": "debit", "amount": 100, "currency": "USD"},
                {"account_id": cash, "direction": "credit", "amount": 100, "currency": "USD"},
            ]
        },
    )
    _assert_problem(response, 422, "/errors/insufficient-funds")
    assert await _row_counts(db_session) == before


async def test_currency_mismatch(app_client: AsyncClient, db_session: AsyncSession) -> None:
    eur = await _create_account(app_client, name="EUR Cash", type="asset", currency="EUR")
    usd_revenue = await _create_account(app_client, name="Revenue", type="revenue", currency="USD")
    before = await _row_counts(db_session)

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": eur, "direction": "debit", "amount": 100, "currency": "USD"},
                {
                    "account_id": usd_revenue,
                    "direction": "credit",
                    "amount": 100,
                    "currency": "USD",
                },
            ]
        },
    )
    _assert_problem(response, 422, "/errors/currency-mismatch")
    assert await _row_counts(db_session) == before


async def test_account_not_found(app_client: AsyncClient, db_session: AsyncSession) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    before = await _row_counts(db_session)

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"},
                {
                    "account_id": str(uuid.uuid4()),
                    "direction": "credit",
                    "amount": 100,
                    "currency": "USD",
                },
            ]
        },
    )
    _assert_problem(response, 404, "/errors/account-not-found")
    assert await _row_counts(db_session) == before


async def test_already_reversed(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    create = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 100, "currency": "USD"},
            ]
        },
    )
    txn_id = create.json()["id"]
    first = await app_client.post(f"/v1/transactions/{txn_id}/reverse")
    assert first.status_code == 201
    second = await app_client.post(f"/v1/transactions/{txn_id}/reverse")
    _assert_problem(second, 409, "/errors/already-reversed")


async def test_single_entry_rejected(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 100, "currency": "USD"}
            ]
        },
    )
    assert response.status_code == 422


async def test_empty_entries_rejected(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/transactions", json={"entries": []})
    assert response.status_code == 422


async def test_zero_amount_rejected(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 0, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 0, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 422


async def test_negative_amount_rejected(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": -1, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": -1, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 422


async def test_validation_error_shape(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/accounts", json={"name": "Cash"})
    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "/errors/validation-error"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "errors" in body
    for err in body["errors"]:
        assert "input" not in err


async def test_unrouted_path_is_problem_json(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
