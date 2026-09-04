"""Shared fixtures for the worker-crash chaos suite.

Reuses `tests/faults/conftest.py`'s fixtures rather than duplicating their
bodies -- `tests/chaos/` is its own directory (pytest fixture discovery
walks up the directory tree from a test's location, and `tests/faults/`
is a sibling, not an ancestor, of `tests/chaos/`), so those fixtures have
to be re-exported here to be visible to tests in this directory. Importing
a `@pytest_asyncio.fixture`-decorated function into a module's namespace is
enough for pytest to register it as a fixture available to tests in that
module/conftest -- the decorator, not the import site, is what makes it a
fixture. `_clean_database` is autouse in `tests/faults/conftest.py`; that
property lives on the function object itself, so it stays autouse for
`tests/chaos/` too once re-exported this way.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.faults.conftest import (  # noqa: F401 -- re-exported for fixture discovery
    _clean_database,
    backdate_claim,
    backdate_lock,
    dispatcher,
    fault_client,
    ledger_row_counts,
    mock_receiver,
    receiver_state,
    wired_delivery,
)


@pytest_asyncio.fixture
async def chaos_client(
    migrated_database_url: str, concurrency_engine: AsyncEngine, clean_database: None
) -> AsyncGenerator[AsyncClient, None]:
    """Same shape as `fault_client`, with one deliberate difference:
    `raise_app_exceptions=False`.

    Starlette's `ServerErrorMiddleware` builds the 500 problem+json response
    from our registered catch-all `Exception` handler *and then re-raises
    the original exception* (so a real ASGI server can still log it) --
    that is standard, correct Starlette behavior, not a bug. `httpx.
    ASGITransport`'s default `raise_app_exceptions=True` exists so ordinary
    tests notice an accidental unhandled exception immediately (see
    `tests/faults/test_idempotency_crash.py`'s `_post_expecting_crash`,
    which deliberately works with that default for a *simulated*
    `BaseException` crash). But these chaos tests need to inspect the
    actual RFC 7807 response body the client would have received on the
    wire, not just prove that something was raised -- so this fixture opts
    out, the one client in the chaos suite that does.
    """
    from tests.support.auth import TEST_API_KEY, seed_api_key

    await seed_api_key(concurrency_engine, raw_key=TEST_API_KEY)

    from ledger.api.main import create_app
    from ledger.db.session import get_session

    session_factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_API_KEY}"},
    ) as client:
        yield client
