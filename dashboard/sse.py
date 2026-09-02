"""Server-sent events for the dashboard (SPEC.md §12 Phase 6): periodic-poll
SSE with per-view change detection, not Postgres LISTEN/NOTIFY -- see
docs/DECISIONS.md for why. Every tick opens and closes its own session
(never one held across `await asyncio.sleep`, which would pin a pooled
connection idle for the life of a browser tab and, in tests, make the next
test's `TRUNCATE` fail against `lock_timeout`), and holds no state shared
across requests -- one poll loop per connection, not a broadcast queue
bound to whichever event loop happened to create it.
"""

import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dashboard.data import PANEL_LOADERS
from dashboard.templating import render_fragment


class Disconnectable(Protocol):
    """Structurally satisfied by `starlette.requests.Request`. A separate,
    minimal protocol -- rather than depending on `Request` directly -- so
    `tests/unit/test_sse_stream.py` can drive `event_stream` with a trivial
    stub instead of constructing a real ASGI request."""

    async def is_disconnected(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class StreamConfig:
    interval_seconds: float
    keepalive_seconds: float = 15.0
    max_stream_seconds: float = 3600.0
    #: `None` streams forever (production). A positive int bounds the
    #: number of poll ticks so a test can drain the generator to
    #: completion instead of abandoning a live one -- see
    #: docs/DECISIONS.md for why that matters under
    #: `filterwarnings = ["error"]`.
    max_events: int | None = None


def format_sse(
    *, event: str | None = None, data: str | None = None, comment: str | None = None
) -> bytes:
    """`data:` cannot contain a raw newline -- a multi-line HTML fragment
    must become one `data:` line per source line, or the stream framing
    breaks. Every frame (event or comment) ends with a blank line, per the
    SSE wire format."""
    lines: list[str] = []
    if comment is not None:
        for part in comment.splitlines() or [""]:
            lines.append(f": {part}")
    if event is not None:
        lines.append(f"event: {event}")
    if data is not None:
        for part in data.splitlines() or [""]:
            lines.append(f"data: {part}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


async def snapshot(session: AsyncSession) -> dict[str, str]:
    """Render every panel's fragment once, from one consistent read. Shared
    by `event_stream` (one call per tick) -- there is no separate "initial
    page" code path; `dashboard/views.py::index` calls the same
    `PANEL_LOADERS` directly instead, since it also needs `demo_enabled` in
    its context."""
    fragments: dict[str, str] = {}
    for name, loader in PANEL_LOADERS.items():
        context = await loader(session)
        fragments[name] = render_fragment(f"partials/_{name}.html", context)
    return fragments


def _digest(html: str) -> str:
    return hashlib.blake2b(html.encode("utf-8"), digest_size=16).hexdigest()


async def event_stream(
    request: Disconnectable,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    config: StreamConfig,
) -> AsyncGenerator[bytes, None]:
    # A comment frame flushes response headers immediately, so a client (and
    # a test) observes 200 + content-type without waiting a full tick.
    yield format_sse(comment="connected")

    stop = asyncio.Event()
    last_digest: dict[str, str] = {}
    last_write = time.monotonic()
    started = last_write
    ticks = 0

    while True:
        if await request.is_disconnected():
            return

        async with session_factory() as session:
            fragments = await snapshot(session)

        emitted = False
        for name, html in fragments.items():
            digest = _digest(html)
            if last_digest.get(name) != digest:
                last_digest[name] = digest
                yield format_sse(event=name, data=html)
                emitted = True
        now = time.monotonic()
        if emitted:
            last_write = now
        elif now - last_write >= config.keepalive_seconds:
            yield format_sse(comment="keep-alive")
            last_write = now

        ticks += 1
        if config.max_events is not None and ticks >= config.max_events:
            return
        if time.monotonic() - started >= config.max_stream_seconds:
            return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=config.interval_seconds)
