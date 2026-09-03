"""Unit tests for `ledger.admin.keys.main`'s argument parsing and dispatch
-- no DB, `asyncio.run` itself is stubbed out so the coroutine each
subcommand builds is inspected rather than awaited."""

import uuid
from collections.abc import Coroutine
from typing import Any

import pytest

from ledger.admin import keys as keys_cli


def _stub_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> list[Coroutine[Any, Any, Any]]:
    calls: list[Coroutine[Any, Any, Any]] = []

    def _fake_run(coro: Coroutine[Any, Any, Any]) -> None:
        calls.append(coro)
        coro.close()  # never actually awaited -- would touch the DB

    monkeypatch.setattr("ledger.admin.keys.asyncio.run", _fake_run)
    return calls


def test_mint_requires_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_asyncio_run(monkeypatch)
    with pytest.raises(SystemExit):
        keys_cli.main(["mint"])


def test_mint_dispatches_with_the_given_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_asyncio_run(monkeypatch)
    keys_cli.main(["mint", "--name", "example"])
    assert len(calls) == 1
    assert calls[0].cr_code.co_name == "_mint"  # type: ignore[attr-defined]


def test_list_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_asyncio_run(monkeypatch)
    keys_cli.main(["list"])
    assert len(calls) == 1
    assert calls[0].cr_code.co_name == "_list"  # type: ignore[attr-defined]


def test_revoke_requires_a_valid_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_asyncio_run(monkeypatch)
    with pytest.raises(SystemExit):
        keys_cli.main(["revoke", "--id", "not-a-uuid"])


def test_revoke_dispatches_with_the_parsed_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_asyncio_run(monkeypatch)
    key_id = uuid.uuid4()
    keys_cli.main(["revoke", "--id", str(key_id)])
    assert len(calls) == 1
    assert calls[0].cr_code.co_name == "_revoke"  # type: ignore[attr-defined]


def test_no_subcommand_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_asyncio_run(monkeypatch)
    with pytest.raises(SystemExit):
        keys_cli.main([])
