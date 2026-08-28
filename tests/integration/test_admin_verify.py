import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.balances import AccountBalance

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_verify_ok_on_clean_ledger(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/admin/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["global_balance"]["ok"] is True


async def test_verify_ok_after_many_postings_and_reversals(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")

    txn_ids = []
    for amount in (100, 200, 300):
        r = await app_client.post(
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
        txn_ids.append(r.json()["id"])
    await app_client.post(f"/v1/transactions/{txn_ids[0]}/reverse")

    response = await app_client.get("/v1/admin/verify")
    assert response.json()["ok"] is True


async def test_verify_detects_corrupted_balance(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id = await _create_account(app_client, name="Cash", type="asset")
    await db_session.execute(
        update(AccountBalance)
        .where(AccountBalance.account_id == uuid.UUID(account_id))
        .values(balance=AccountBalance.balance + 1)
    )
    await db_session.commit()

    response = await app_client.get("/v1/admin/verify")
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    corrupted = [a for a in body["accounts"] if a["account_id"] == account_id]
    assert len(corrupted) == 1
    assert corrupted[0]["ok"] is False
    assert corrupted[0]["stored_balance"] == 1
    assert corrupted[0]["derived_balance"] == 0


async def test_verify_detects_global_imbalance(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    # Insert a lone debit entry directly -- the append-only trigger only
    # blocks UPDATE/DELETE, not INSERT.
    txn_id = (
        await db_session.execute(
            text("INSERT INTO transactions (source) VALUES ('api') RETURNING id")
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO entries (transaction_id, account_id, direction, amount, currency) "
            "VALUES (:t, :a, 'debit', 100, 'USD')"
        ),
        {"t": txn_id, "a": uuid.UUID(cash)},
    )
    await db_session.commit()

    response = await app_client.get("/v1/admin/verify")
    body = response.json()
    assert body["ok"] is False
    assert body["global_balance"]["ok"] is False


async def test_verify_is_read_only(app_client: AsyncClient, db_session: AsyncSession) -> None:
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
    before = (await db_session.execute(text("SELECT COUNT(*) FROM entries"))).scalar_one()
    await app_client.get("/v1/admin/verify")
    after = (await db_session.execute(text("SELECT COUNT(*) FROM entries"))).scalar_one()
    assert before == after
