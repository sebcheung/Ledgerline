"""Genuinely terminate the Postgres backend serving a `POST /v1/transactions`
request while `ledger.core.posting.post_transaction` is mid-write, before
its transaction commits.

Injection point: `posting._lock_account_balances`, the function
`post_transaction` calls immediately after step 3's ordered lock (see
`ledger.core.posting`'s module docstring and `LockHook`). We wrap it so
that, right after it acquires the real `FOR UPDATE` locks, it reads back
`pg_backend_pid()` on the request's own connection and kills that exact
backend from an independent connection (`admin_engine`) -- a hard
disconnect strictly *inside* the ledger write, well before the entries
insert, the balance update, or the outbox event, and well before commit.

This is real infrastructure chaos, not a monkeypatched exception: nothing
about `post_transaction`'s control flow is altered, only the backend
serving its connection is pulled out from under it. What we are proving is
that Postgres's own transactional rollback-on-disconnect behavior -- not
any application-level try/except -- is what keeps a killed mid-write from
leaving a half-posted transaction behind.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.core import posting as posting_mod
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


async def test_db_loss_mid_posting_leaves_no_partial_transaction(
    chaos_client: AsyncClient,
    admin_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cash = await _create_account(chaos_client, name="Cash", type="asset")
    revenue = await _create_account(chaos_client, name="Revenue", type="revenue")
    body = await _transfer_body(cash, revenue)
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

    # chaos_client's transport has raise_app_exceptions=False (unlike the
    # rest of the suite's fault_client): ServerErrorMiddleware re-raises
    # after building the 500 response so a real ASGI server can still log
    # it, and httpx's default would surface that re-raise to us instead of
    # the response it already sent -- we need the actual response body
    # here, not just proof that something was raised.
    response = await chaos_client.post("/v1/transactions", json=body)

    # A raw OperationalError from a dead backend must not escape as an
    # unhandled-exception traceback -- ledger.api.errors's catch-all
    # `Exception` handler (registered in register_error_handlers) already
    # maps any exception that isn't one of the more specific handlers to a
    # clean RFC 7807 problem+json 500, and that is exactly what a killed
    # connection is: just another `Exception`, never a `BaseException`, so
    # it takes the same path as any other unexpected server-side failure.
    assert response.status_code >= 500
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["status"] == response.status_code
    assert problem["type"]
    assert problem["title"]

    # The proof: Postgres's own transactional rollback-on-disconnect is
    # what protects the invariant here, not any code we wrote. Read this
    # back from `db_session` (a fresh connection), never from the killed
    # one.
    assert await ledger_row_counts(db_session) == before
