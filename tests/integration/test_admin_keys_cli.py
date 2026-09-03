"""Integration tests for `ledger.admin.keys` (SPEC.md §9 Phase 7).

Exercises the async `_mint`/`_list`/`_revoke` implementations directly
rather than the sync `main()` entry point: `main()` wraps each in
`asyncio.run(...)`, which cannot be called from inside the event loop an
`async def` test already runs in. `tests/unit/test_admin_keys_cli_args.py`
covers `main()`'s argument parsing and dispatch in isolation.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.apikeys import hash_api_key
from ledger.models.api_keys import ApiKey

pytestmark = pytest.mark.integration


async def test_mint_prints_a_key_whose_hash_is_stored(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: AsyncSession,
) -> None:
    from ledger.admin import keys as keys_cli

    # `_mint` opens its own session via `async_session_factory()` and
    # disposes the module-level engine on exit -- neither is safe to let
    # touch the real production engine from a test, so both are patched:
    # the session factory hands back the test's own open
    # session/transaction, and engine disposal is a no-op.
    monkeypatch.setattr(
        "ledger.admin.keys.async_session_factory", lambda: _NoCloseSession(db_session)
    )
    monkeypatch.setattr("ledger.admin.keys.engine", _NoopDisposeEngine())

    await keys_cli._mint("ci-test")

    out = capsys.readouterr().out
    assert "key: lk_" in out
    assert "will not be shown again" in out

    raw_key = next(line for line in out.splitlines() if line.startswith("key: "))[len("key: ") :]
    row = (
        await db_session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key)))
    ).scalar_one()
    assert row.name == "ci-test"
    assert row.active is True


async def test_list_and_revoke_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: AsyncSession,
) -> None:
    from ledger.admin import keys as keys_cli

    monkeypatch.setattr(
        "ledger.admin.keys.async_session_factory", lambda: _NoCloseSession(db_session)
    )
    monkeypatch.setattr("ledger.admin.keys.engine", _NoopDisposeEngine())

    await keys_cli._mint("round-trip")
    out = capsys.readouterr().out
    key_id = uuid.UUID(next(line for line in out.splitlines() if line.startswith("id: "))[4:])

    await keys_cli._list()
    listing = capsys.readouterr().out
    assert "round-trip" in listing
    assert "active" in listing

    await keys_cli._revoke(key_id)
    revoked_out = capsys.readouterr().out
    assert f"revoked {key_id}" in revoked_out

    row = (await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()
    assert row.active is False


async def test_revoke_unknown_id_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db_session: AsyncSession,
) -> None:
    from ledger.admin import keys as keys_cli

    monkeypatch.setattr(
        "ledger.admin.keys.async_session_factory", lambda: _NoCloseSession(db_session)
    )
    monkeypatch.setattr("ledger.admin.keys.engine", _NoopDisposeEngine())

    with pytest.raises(SystemExit):
        await keys_cli._revoke(uuid.uuid4())
    assert "no active key" in capsys.readouterr().out


class _NoCloseSession:
    """Wraps a real, already-open test `AsyncSession` so `async with
    async_session_factory() as session:` in `ledger.admin.keys` gets the
    test's own transaction instead of opening (and closing) a second real
    connection against the production engine."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _NoopDisposeEngine:
    """Replaces `ledger.admin.keys.engine` for the duration of a test --
    `AsyncEngine.dispose` can't be monkeypatched on the real singleton
    (it's a read-only bound method), and disposing the real engine from a
    test would tear down the connection pool every other fixture shares."""

    async def dispose(self) -> None:
        return None
