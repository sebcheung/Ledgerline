"""SPEC.md §10: key reuse with a different body, and endpoint scope conflicts."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.faults.conftest import backdate_lock, ledger_row_counts

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


async def test_same_key_different_body_after_completion_is_key_reuse(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    key = str(uuid.uuid4())

    first = await fault_client.post(
        "/v1/transactions",
        json=await _transfer_body(cash, revenue, 1000),
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201

    second = await fault_client.post(
        "/v1/transactions",
        json=await _transfer_body(cash, revenue, 2000),
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 422
    assert second.json()["type"] == "/errors/idempotency-key-reuse"

    assert await ledger_row_counts(db_session) == (1, 2, 1)


async def test_same_key_different_body_against_stale_lock_is_key_reuse_not_reclaim(
    fault_client: AsyncClient, db_session: AsyncSession, concurrency_engine: object
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    key = str(uuid.uuid4())

    # Claim the key via a request that we let complete, then rewrite its
    # lock to look stale -- the mismatch is what matters here, not whether
    # the row is "in_progress" or "completed"; ledger.core.idempotency
    # checks fingerprint equality on both branches (SPEC.md §6).
    first = await fault_client.post(
        "/v1/transactions",
        json=await _transfer_body(cash, revenue, 1000),
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    await backdate_lock(concurrency_engine, key, 31)  # type: ignore[arg-type]

    second = await fault_client.post(
        "/v1/transactions",
        json=await _transfer_body(cash, revenue, 2000),
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 422
    assert second.json()["type"] == "/errors/idempotency-key-reuse"
    assert await ledger_row_counts(db_session) == (1, 2, 1)


async def test_key_used_on_transactions_then_reverse_is_scope_conflict(
    fault_client: AsyncClient,
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    key = str(uuid.uuid4())

    posted = await fault_client.post(
        "/v1/transactions",
        json=await _transfer_body(cash, revenue),
        headers={"Idempotency-Key": key},
    )
    assert posted.status_code == 201

    reused = await fault_client.post(
        f"/v1/transactions/{posted.json()['id']}/reverse", headers={"Idempotency-Key": key}
    )
    assert reused.status_code == 422
    assert reused.json()["type"] == "/errors/idempotency-key-scope-conflict"


async def test_same_key_on_reverse_for_two_different_transactions_is_key_reuse(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Pins the path-params-in-fingerprint decision: POST .../reverse has an
    empty body, so without folding the transaction id into the fingerprint
    every reversal would hash identically and this would wrongly replay."""
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    txn_a = (
        await fault_client.post("/v1/transactions", json=await _transfer_body(cash, revenue))
    ).json()
    txn_b = (
        await fault_client.post("/v1/transactions", json=await _transfer_body(cash, revenue))
    ).json()

    key = str(uuid.uuid4())
    first = await fault_client.post(
        f"/v1/transactions/{txn_a['id']}/reverse", headers={"Idempotency-Key": key}
    )
    assert first.status_code == 201

    second = await fault_client.post(
        f"/v1/transactions/{txn_b['id']}/reverse", headers={"Idempotency-Key": key}
    )
    assert second.status_code == 422
    assert second.json()["type"] == "/errors/idempotency-key-reuse"

    # txn_a, txn_b, and one reversal (of a) -- txn_b's reversal never ran.
    txns, _entries, _outbox = await ledger_row_counts(db_session)
    assert txns == 3


async def test_explicit_null_field_replays_rather_than_reuses(
    fault_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Key-order/whitespace/null-member independence is covered at the unit
    level (test_canonical_hash.py); this only pins the one variant that's
    ambiguous over HTTP -- an explicit `"description": null` must replay,
    not reuse, since it canonicalizes the same as omitting the field."""
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    key = str(uuid.uuid4())

    first = await fault_client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201

    variant = {"description": None, **body}
    second = await fault_client.post(
        "/v1/transactions", json=variant, headers={"Idempotency-Key": key}
    )
    assert second.status_code == 201
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert await ledger_row_counts(db_session) == (1, 2, 1)
