"""Unit tests for `dashboard.sse.event_stream` -- driven with a stub
session factory and a stub request, no DB and no ASGI machinery. Proves the
generator's own logic (initial snapshot, change detection, heartbeat,
tick-bounding, clean cancellation) independently of the wiring
`tests/integration/test_dashboard_sse.py` covers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from dashboard.sse import StreamConfig, event_stream


class _NeverDisconnects:
    async def is_disconnected(self) -> bool:
        return False


class _StubSession:
    """Not a real `AsyncSession` -- `event_stream` never calls anything on
    it directly; it's only ever handed to `dashboard.data`'s panel loaders
    via the session factory below, which this test replaces entirely."""


def _make_session_factory() -> Any:
    """A zero-arg callable returning an async context manager -- the shape
    `event_stream`'s `async with session_factory() as session` expects.
    What it yields is never inspected: `dashboard.sse.snapshot` is
    monkeypatched below to return canned fragments per tick instead of
    actually querying, so this only needs to open and close cleanly."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[_StubSession]:
        yield _StubSession()

    return _factory


@pytest.fixture(autouse=True)
def _patch_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = [
        {"balances": "<p>a</p>", "transactions": "<p>b</p>"},
        {"balances": "<p>a</p>", "transactions": "<p>b</p>"},  # unchanged
        {"balances": "<p>a2</p>", "transactions": "<p>b</p>"},  # balances changed
    ]
    call_index = {"n": 0}

    async def _fake_snapshot(_session: object) -> dict[str, str]:
        i = min(call_index["n"], len(calls) - 1)
        call_index["n"] += 1
        return calls[i]

    monkeypatch.setattr("dashboard.sse.snapshot", _fake_snapshot)


async def test_first_tick_emits_every_panel() -> None:
    frames = [
        f
        async for f in event_stream(
            _NeverDisconnects(),
            session_factory=_make_session_factory(),
            config=StreamConfig(interval_seconds=0.0, max_events=1),
        )
    ]
    text = b"".join(frames).decode("utf-8")
    assert text.startswith(": connected\n\n")
    assert "event: balances" in text
    assert "event: transactions" in text


async def test_unchanged_tick_emits_only_a_heartbeat() -> None:
    frames = [
        f
        async for f in event_stream(
            _NeverDisconnects(),
            session_factory=_make_session_factory(),
            config=StreamConfig(interval_seconds=0.0, keepalive_seconds=0.0, max_events=2),
        )
    ]
    text = b"".join(frames).decode("utf-8")
    assert text.count("event: balances") == 1
    assert text.count("event: transactions") == 1
    assert ": keep-alive" in text


async def test_third_tick_emits_only_the_changed_panel() -> None:
    frames = [
        f
        async for f in event_stream(
            _NeverDisconnects(),
            session_factory=_make_session_factory(),
            config=StreamConfig(interval_seconds=0.0, keepalive_seconds=0.0, max_events=3),
        )
    ]
    text = b"".join(frames).decode("utf-8")
    assert text.count("event: balances") == 2  # tick 1 (initial) and tick 3 (changed)
    assert text.count("event: transactions") == 1  # only tick 1 -- never changes


async def test_max_events_bounds_the_generator_to_completion() -> None:
    gen = event_stream(
        _NeverDisconnects(),
        session_factory=_make_session_factory(),
        config=StreamConfig(interval_seconds=0.0, max_events=1),
    )
    frames = [f async for f in gen]
    assert len(frames) >= 1
    # The generator must have returned on its own -- a second `anext` call
    # raises StopAsyncIteration rather than hanging.
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_disconnected_request_ends_the_stream_immediately() -> None:
    class _AlreadyDisconnected:
        async def is_disconnected(self) -> bool:
            return True

    frames = [
        f
        async for f in event_stream(
            _AlreadyDisconnected(),
            session_factory=_make_session_factory(),
            config=StreamConfig(interval_seconds=0.0, max_events=5),
        )
    ]
    # Only the initial "connected" comment -- the loop checks disconnection
    # before doing any work on its first iteration.
    assert frames == [b": connected\n\n"]


async def test_generator_aclose_mid_iteration_does_not_raise() -> None:
    gen = event_stream(
        _NeverDisconnects(),
        session_factory=_make_session_factory(),
        config=StreamConfig(interval_seconds=10.0),  # never fires on its own
    )
    await gen.__anext__()
    await gen.aclose()
