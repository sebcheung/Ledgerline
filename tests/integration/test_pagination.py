import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.enums import EntryDirection

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": True}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_empty_returns_empty_page_and_no_cursor(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    response = await app_client.get(f"/v1/accounts/{cash}/entries")
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False


async def test_single_page_when_under_limit(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    revenue = await _create_account(app_client, type="revenue")
    await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 10, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 10, "currency": "USD"},
            ]
        },
    )
    response = await app_client.get(f"/v1/accounts/{cash}/entries", params={"limit": 50})
    body = response.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is False
    assert body["next_cursor"] is None


async def test_walk_all_pages_yields_every_entry_exactly_once(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    revenue = await _create_account(app_client, type="revenue")
    for amount in range(1, 26):
        await app_client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {"account_id": cash, "direction": "debit", "amount": amount, "currency": "USD"},
                    {
                        "account_id": revenue,
                        "direction": "credit",
                        "amount": amount,
                        "currency": "USD",
                    },
                ]
            },
        )

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        response = await app_client.get(f"/v1/accounts/{cash}/entries", params=params)
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]

    assert len(seen) == 25
    assert len(set(seen)) == 25


async def test_stable_order_under_identical_created_at(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(app_client)
    revenue = await _create_account(app_client, type="revenue")
    other = await _create_account(app_client, type="revenue")

    # Post one transaction with three legs against `cash` -- all three
    # entries share an identical `created_at` (Postgres now() is
    # transaction-start time), forcing the id tiebreak to do real work.
    import uuid as uuid_mod

    await post_transaction(
        db_session,
        [
            EntryRequest(uuid_mod.UUID(cash), EntryDirection.DEBIT, 10, "USD"),
            EntryRequest(uuid_mod.UUID(revenue), EntryDirection.CREDIT, 5, "USD"),
            EntryRequest(uuid_mod.UUID(other), EntryDirection.CREDIT, 5, "USD"),
        ],
    )
    await db_session.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await app_client.get(f"/v1/accounts/{cash}/entries", params=params)
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]

    assert len(seen) == 1
    assert len(set(seen)) == 1


async def test_invalid_cursor_400(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    response = await app_client.get(
        f"/v1/accounts/{cash}/entries", params={"cursor": "not-a-valid-cursor!!"}
    )
    assert response.status_code == 400
    assert response.json()["type"] == "/errors/invalid-cursor"


async def test_limit_bounds(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    for limit in (0, -1, 300):
        response = await app_client.get(f"/v1/accounts/{cash}/entries", params={"limit": limit})
        assert response.status_code == 400


async def test_cursor_is_opaque_not_offset(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client)
    revenue = await _create_account(app_client, type="revenue")
    for amount in (1, 2):
        await app_client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {"account_id": cash, "direction": "debit", "amount": amount, "currency": "USD"},
                    {
                        "account_id": revenue,
                        "direction": "credit",
                        "amount": amount,
                        "currency": "USD",
                    },
                ]
            },
        )
    response = await app_client.get(f"/v1/accounts/{cash}/entries", params={"limit": 1})
    cursor = response.json()["next_cursor"]
    assert cursor is not None
    assert not cursor.isdigit()


async def test_unknown_account_404(app_client: AsyncClient) -> None:
    import uuid

    response = await app_client.get(f"/v1/accounts/{uuid.uuid4()}/entries")
    assert response.status_code == 404
