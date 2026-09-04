"""Same real-backend-kill technique as `test_db_loss_mid_posting.py`, but
timed to land *after* `ledger.core.idempotency.claim_key` has already
committed its claim and *before* the request completes -- composing with
the crash-recovery contract `tests/faults/test_idempotency_crash.py`
already established for a simulated (`BaseException`) crash at the same
logical point.

`claim_key` commits on its own (see its module docstring: "the claim
commits; everything else obeys the caller-commits rule"), on the *same*
connection the rest of the request keeps using. So by the time our
`_lock_account_balances` injection point kills that connection's backend,
the `in_progress` row is already durable -- killing the connection cannot
un-claim it.

What actually happens next turns out to be *better* than the naive
prediction that the key is simply left stuck: SQLAlchemy's
`AsyncSession.rollback()` on a connection whose backend was killed
recognizes the connection is unusable and discards it rather than raising,
and the very next statement `release_key` issues (its own `DELETE` +
`commit`) transparently checks out a brand-new connection from the pool
(NullPool, via `concurrency_engine`) to do it -- confirmed empirically
below to be deterministic across repeated runs, not a lucky race. So the
key is actually released cleanly, and an *immediate* retry (no TTL
backdating needed) claims it fresh and succeeds exactly once. This is a
stronger result than `tests/faults/test_idempotency_crash.py`'s simulated
`BaseException` crash, which bypasses `IdempotentRequest.run`'s
`except Exception` cleanup entirely (a `BaseException` is deliberately not
caught by it) and therefore *does* leave the key stuck `in_progress` until
its TTL expires -- proof that it is specifically the cleanup path's own
resilience (not something about this class of fault in general) that
avoids the stuck lock here.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.core import posting as posting_mod
from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey
from tests.chaos._pg_kill import backend_pid, kill_backend
from tests.faults.conftest import ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.chaos, pytest.mark.timeout(60)]


async def _create_account(client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def _transfer_body(cash: str, revenue: str) -> dict[str, object]:
    return {
        "entries": [
            {"account_id": cash, "direction": "debit", "amount": 1000, "currency": "USD"},
            {"account_id": revenue, "direction": "credit", "amount": 1000, "currency": "USD"},
        ]
    }


async def _key_status(session: AsyncSession, key: str) -> IdempotencyStatus | None:
    row = (
        await session.execute(select(IdempotencyKey.status).where(IdempotencyKey.key == key))
    ).one_or_none()
    return IdempotencyStatus(row.status) if row is not None else None


async def test_db_loss_after_claim_is_cleanly_released_and_recovers(
    chaos_client: AsyncClient,
    admin_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cash = await _create_account(chaos_client, name="Cash", type="asset")
    revenue = await _create_account(chaos_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    key = str(uuid.uuid4())
    before = await ledger_row_counts(db_session)

    real_lock = posting_mod._lock_account_balances

    async def killing_lock(
        session: AsyncSession, account_ids: list[object], *, _lock_hook: object
    ) -> object:
        rows = await real_lock(session, account_ids, _lock_hook=_lock_hook)  # type: ignore[arg-type]
        pid = await backend_pid(session)
        assert await kill_backend(admin_engine, pid), "target backend was already gone"
        return rows

    monkeypatch.setattr(posting_mod, "_lock_account_balances", killing_lock)

    response = await chaos_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert response.status_code >= 500

    monkeypatch.undo()

    # The claim really did commit before the kill -- it is durable, and the
    # kill could not have un-claimed it. Nothing about the ledger write
    # itself made it through, though.
    assert await ledger_row_counts(db_session) == before

    # The key was cleanly released by IdempotentRequest.run's own cleanup
    # path (see module docstring) -- not left stuck in_progress.
    assert await _key_status(db_session, key) is None

    # Because the key was actually released, an immediate retry with the
    # same key claims it fresh and succeeds -- no TTL wait, no backdating,
    # exactly one ledger effect.
    recovered = await chaos_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert recovered.status_code == 201
    assert await ledger_row_counts(db_session) == (before[0] + 1, before[1] + 2, before[2] + 1)
    assert await _key_status(db_session, key) == IdempotencyStatus.COMPLETED

    # And a further replay of the same key/body is served from the stored
    # response, not re-executed.
    replay = await chaos_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert await ledger_row_counts(db_session) == (before[0] + 1, before[1] + 2, before[2] + 1)
