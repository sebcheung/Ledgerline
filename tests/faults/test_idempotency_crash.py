"""SPEC.md §10: crash between the ledger write and key completion.

Simulated, not a real process kill: `SimulatedCrash` is a `BaseException`
(not `Exception`), which slips past `IdempotentRequest.run`'s `except
Exception` release path exactly the way a SIGKILL would -- nothing runs
after the crash point, no cleanup, no handler. This is deterministic and
instant, unlike actually killing a process, and it exercises the same
invariant: whatever commit never happened leaves no trace.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey
from tests.faults.conftest import backdate_lock, ledger_row_counts

pytestmark = [pytest.mark.integration, pytest.mark.fault]


class SimulatedCrash(BaseException):
    pass


async def _post_expecting_crash(client: AsyncClient, url: str, **kwargs: object) -> None:
    """`RequestIdMiddleware` is Starlette `BaseHTTPMiddleware`, which runs
    the inner app in a background task group; when that task raises a
    `BaseException`, anyio surfaces it wrapped in a `BaseExceptionGroup`
    alongside a secondary `RuntimeError("No response returned.")` -- not
    the bare `SimulatedCrash` a direct call would raise. Unwrap and assert
    on that shape instead of the raw exception type."""
    with pytest.raises(BaseExceptionGroup) as exc_info:
        await client.post(url, **kwargs)  # type: ignore[arg-type]
    assert any(isinstance(e, SimulatedCrash) for e in exc_info.value.exceptions)


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


async def test_crash_before_key_completion_leaves_no_ledger_effect(
    fault_client: AsyncClient,
    db_session: AsyncSession,
    concurrency_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    key = str(uuid.uuid4())

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise SimulatedCrash("simulated crash after the ledger write, before key completion")

    monkeypatch.setattr("ledger.api.idempotent.complete_key", boom)

    await _post_expecting_crash(
        fault_client, "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )

    assert await ledger_row_counts(db_session) == (0, 0, 0)
    assert await _key_status(db_session, key) == IdempotencyStatus.IN_PROGRESS

    monkeypatch.undo()

    # Immediate retry: the lock is still fresh, so this must be blocked,
    # not silently re-executed.
    blocked = await fault_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert blocked.status_code == 409
    assert blocked.headers["Retry-After"] == "1"
    assert await ledger_row_counts(db_session) == (0, 0, 0)

    # Backdate past the TTL: the retry reclaims and executes cleanly.
    await backdate_lock(concurrency_engine, key, 31)
    recovered = await fault_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert recovered.status_code == 201
    assert await ledger_row_counts(db_session) == (1, 2, 1)
    assert await _key_status(db_session, key) == IdempotencyStatus.COMPLETED


async def test_crash_before_commit_rolls_back_ledger_write_and_completion_together(
    fault_client: AsyncClient,
    db_session: AsyncSession,
    concurrency_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demonstrates the crux directly: complete_key's UPDATE and the ledger
    write share one commit, so a crash between the UPDATE and the commit
    rolls both back together, exactly like a crash before the UPDATE ran
    at all."""
    cash = await _create_account(fault_client, name="Cash", type="asset")
    revenue = await _create_account(fault_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
    key = str(uuid.uuid4())

    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    real_commit = SAAsyncSession.commit
    call_count = {"n": 0}

    async def flaky_commit(self: SAAsyncSession) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1st commit is the claim; 2nd is the crux
            raise SimulatedCrash("simulated crash between completion and commit")
        await real_commit(self)

    monkeypatch.setattr(SAAsyncSession, "commit", flaky_commit)

    await _post_expecting_crash(
        fault_client, "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )

    monkeypatch.undo()

    assert await ledger_row_counts(db_session) == (0, 0, 0)
    assert await _key_status(db_session, key) == IdempotencyStatus.IN_PROGRESS

    await backdate_lock(concurrency_engine, key, 31)
    recovered = await fault_client.post(
        "/v1/transactions", json=body, headers={"Idempotency-Key": key}
    )
    assert recovered.status_code == 201
    assert await ledger_row_counts(db_session) == (1, 2, 1)
