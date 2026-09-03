"""Integration test for `ledger.admin.sweep` (SPEC.md §9 Phase 7).

Exercises the async `_sweep` implementation directly, same reasoning as
`tests/integration/test_admin_keys_cli.py`: `main()`'s `asyncio.run(...)`
cannot run from inside an already-running event loop.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.enums import IdempotencyStatus
from ledger.models.idempotency import IdempotencyKey

pytestmark = pytest.mark.integration


class _NoCloseSession:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _NoopDisposeEngine:
    async def dispose(self) -> None:
        return None


async def test_sweep_cli_deletes_old_completed_keys_and_reports_the_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: AsyncSession,
) -> None:
    from ledger.admin import sweep as sweep_cli

    monkeypatch.setattr(
        "ledger.admin.sweep.async_session_factory", lambda: _NoCloseSession(db_session)
    )
    monkeypatch.setattr("ledger.admin.sweep.engine", _NoopDisposeEngine())

    old = datetime.now(UTC) - timedelta(days=40)
    await db_session.execute(
        insert(IdempotencyKey).values(
            key="cli-old",
            endpoint="POST /v1/transactions",
            request_fingerprint="fp",
            status=IdempotencyStatus.COMPLETED,
            locked_at=old,
            created_at=old,
        )
    )
    await db_session.commit()

    await sweep_cli._sweep(30)

    out = capsys.readouterr().out
    assert "deleted 1 completed idempotency key" in out
    remaining = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "cli-old"))
    ).first()
    assert remaining is None
