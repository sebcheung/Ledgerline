import uuid
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection
from tests.conftest import AccountSnapshot

pytestmark = pytest.mark.integration

AccountFactory = Callable[..., Awaitable[AccountSnapshot]]


async def test_create_account_201_returns_body(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/v1/accounts",
        json={"name": "Cash", "type": "asset", "currency": "USD", "allow_negative": False},
    )
    assert response.status_code == 201
    assert "Location" in response.headers
    body = response.json()
    assert body["name"] == "Cash"
    assert body["type"] == "asset"
    assert body["currency"] == "USD"
    assert body["allow_negative"] is False
    assert body["balance"] == 0
    assert body["entry_count"] == 0
    uuid.UUID(body["id"])  # does not raise


async def test_create_account_creates_zero_balance_row(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await app_client.post(
        "/v1/accounts", json={"name": "Cash", "type": "asset", "currency": "USD"}
    )
    account_id = uuid.UUID(response.json()["id"])
    row = (
        await db_session.execute(
            select(
                AccountBalance.balance, AccountBalance.entry_count, AccountBalance.currency
            ).where(AccountBalance.account_id == account_id)
        )
    ).one()
    assert (row.balance, row.entry_count, row.currency) == (0, 0, "USD")


async def test_create_account_persists_across_sessions(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/v1/accounts", json={"name": "Cash", "type": "asset", "currency": "USD"}
    )
    account_id = response.json()["id"]

    response2 = await app_client.get(f"/v1/accounts/{account_id}")
    assert response2.status_code == 200
    assert response2.json()["id"] == account_id


@pytest.mark.parametrize("currency", ["US", "usdollar", "", "123", "usd"])
async def test_create_account_rejects_bad_currency(app_client: AsyncClient, currency: str) -> None:
    response = await app_client.post(
        "/v1/accounts", json={"name": "Cash", "type": "asset", "currency": currency}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_create_account_rejects_unknown_type(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/v1/accounts", json={"name": "Cash", "type": "crypto", "currency": "USD"}
    )
    assert response.status_code == 422


async def test_get_account_includes_balance(
    app_client: AsyncClient, db_session: AsyncSession, account_factory: AccountFactory
) -> None:
    cash = await account_factory(type=AccountType.ASSET, currency="USD")
    revenue = await account_factory(type=AccountType.REVENUE, currency="USD")
    await post_transaction(
        db_session,
        [
            EntryRequest(cash.id, EntryDirection.DEBIT, 500, "USD"),
            EntryRequest(revenue.id, EntryDirection.CREDIT, 500, "USD"),
        ],
    )
    await db_session.commit()

    response = await app_client.get(f"/v1/accounts/{cash.id}")
    assert response.status_code == 200
    assert response.json()["balance"] == 500
    assert response.json()["entry_count"] == 1


async def test_get_account_unknown_id_404_problem_json(app_client: AsyncClient) -> None:
    response = await app_client.get(f"/v1/accounts/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "/errors/account-not-found"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_get_account_malformed_uuid_422(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/accounts/not-a-uuid")
    assert response.status_code == 422


async def test_second_suspense_account_same_currency_conflict(app_client: AsyncClient) -> None:
    await app_client.post(
        "/v1/accounts",
        json={"name": "Suspense USD", "type": "asset", "currency": "USD", "is_suspense": True},
    )
    response = await app_client.post(
        "/v1/accounts",
        json={"name": "Suspense USD 2", "type": "asset", "currency": "USD", "is_suspense": True},
    )
    assert response.status_code == 409
    assert response.json()["type"] == "/errors/suspense-account-exists"
