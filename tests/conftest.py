import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, settings
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.support.db import truncate_all

if TYPE_CHECKING:
    from ledger.models.enums import AccountType

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every property-based test round-trips to Postgres, so a fixed-time deadline
# would produce pure flake under CI I/O jitter -- deadline=None everywhere.
# function_scoped_fixture is expected: fixtures like db_engine/session_factory
# are reused across all examples of one test by design (each example still
# gets a fresh truncate + fresh sessions).
settings.register_profile(
    "dev",
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.register_profile(
    "nightly",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


def _alembic_config(database_url: str) -> Config:
    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations"))
    os.environ["DATABASE_URL"] = database_url
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Generator[str, None, None]:
    """Use DATABASE_URL from the environment (CI service container) when set;
    otherwise spin up a throwaway Postgres via testcontainers for local dev."""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as pg:
        sync_url = pg.get_connection_url()
        async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        os.environ["DATABASE_URL"] = async_url
        yield async_url


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    from ledger.config import get_settings

    get_settings.cache_clear()
    command.upgrade(_alembic_config(database_url), "head")
    return database_url


@pytest_asyncio.fixture
async def db_engine(migrated_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(migrated_database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def admin_engine(migrated_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """A dedicated single-connection engine used only for inter-test cleanup,
    so truncation never contends with whatever the test itself is pooling.

    Function-scoped, not session-scoped: pytest-asyncio gives each test
    function its own event loop by default, and asyncpg connections are
    bound to the loop they were created on. A session-scoped engine would
    hand out connections created on a stale loop to a later test's loop,
    surfacing as opaque "another operation is in progress" errors.
    """
    engine = create_async_engine(migrated_database_url, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def clean_database(admin_engine: AsyncEngine) -> None:
    """Truncate every ledger table. Not autouse here: unit tests never touch
    the database and must not force testcontainers/Alembic to spin up just
    to satisfy a root-level autouse fixture. `tests/integration/conftest.py`
    and `tests/property/conftest.py` wrap this in an autouse fixture scoped
    to only those directories.

    Runs at *setup* (fixtures run before the test body), not teardown: a
    test that crashes or is killed mid-run cannot poison the next test, a
    failure's rows are left in place for post-mortem inspection, and it
    self-heals against any pre-existing garbage left by an earlier failed
    run.
    """
    await truncate_all(admin_engine)


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        finally:
            # A test that raises mid-transaction must not leave an
            # idle-in-transaction connection holding locks -- the next
            # test's TRUNCATE needs ACCESS EXCLUSIVE and would hang on it.
            await session.rollback()


@pytest_asyncio.fixture
async def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker, for tests that need more than one independent session
    (e.g. asserting a route committed by reading from a second session)."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def concurrency_engine(migrated_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """NullPool guarantees every session gets a genuinely fresh connection --
    and therefore a genuinely separate Postgres backend/transaction -- which
    is the property the concurrency and deadlock-order tests depend on. The
    default pool (5 + 10 overflow) would silently serialize a 50-way test."""
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


def _build_app_client(db_engine: AsyncEngine, *, headers: dict[str, str]) -> AsyncClient:
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                # Mirrors db_session: never leave a route's session dangling
                # in an open transaction after the request completes. Routes
                # are responsible for their own commit; this only guards
                # against a route that forgot to, or that raised.
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


@pytest_asyncio.fixture
async def app_client(
    migrated_database_url: str, db_engine: AsyncEngine, clean_database: None
) -> AsyncGenerator[AsyncClient, None]:
    """Phase 7: every `/v1` route requires a Bearer API key, so this fixture
    seeds one and sends it on every request -- existing tests written before
    Phase 7 keep passing unchanged, and now genuinely exercise auth. Depends
    explicitly on `clean_database` (rather than relying on autouse ordering)
    so the seed provably runs after the truncate."""
    from tests.support.auth import TEST_API_KEY, seed_api_key

    await seed_api_key(db_engine, raw_key=TEST_API_KEY)
    async with _build_app_client(
        db_engine, headers={"Authorization": f"Bearer {TEST_API_KEY}"}
    ) as client:
        yield client


@pytest_asyncio.fixture
async def unauthenticated_client(
    migrated_database_url: str, db_engine: AsyncEngine, clean_database: None
) -> AsyncGenerator[AsyncClient, None]:
    """Same app as `app_client`, but with no `Authorization` header --
    for asserting the auth-failure shapes themselves
    (`tests/integration/test_auth.py`)."""
    async with _build_app_client(db_engine, headers={}) as client:
        yield client


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """A detached, immutable view of a created account. Deliberately not the
    live ORM instance: `expire_on_commit=False` means a held `Account`
    object can silently show stale attribute values after another
    connection writes to the row, which is exactly the trap a concurrency
    test must not fall into."""

    id: uuid.UUID
    name: str
    type: "AccountType"
    currency: str
    allow_negative: bool
    is_suspense: bool
    is_clearing: bool


@pytest_asyncio.fixture
async def account_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[AccountSnapshot]]:
    """Insert an account and its `account_balances` row atomically, mirroring
    the real `POST /v1/accounts` contract (pinned separately by
    `test_create_account_creates_zero_balance_row`). Uses its own session and
    commits immediately, so callers see the account from any other session."""
    from ledger.models.accounts import Account
    from ledger.models.balances import AccountBalance
    from ledger.models.enums import AccountType

    async def make(
        *,
        name: str = "Test Account",
        type: AccountType = AccountType.ASSET,
        currency: str = "USD",
        allow_negative: bool = False,
        is_suspense: bool = False,
        is_clearing: bool = False,
    ) -> AccountSnapshot:
        async with session_factory() as session:
            row = (
                await session.execute(
                    insert(Account)
                    .values(
                        name=name,
                        type=type,
                        currency=currency,
                        allow_negative=allow_negative,
                        is_suspense=is_suspense,
                        is_clearing=is_clearing,
                    )
                    .returning(Account.id)
                )
            ).one()
            await session.execute(
                insert(AccountBalance).values(
                    account_id=row.id, currency=currency, balance=0, entry_count=0
                )
            )
            await session.commit()
            return AccountSnapshot(
                id=row.id,
                name=name,
                type=type,
                currency=currency,
                allow_negative=allow_negative,
                is_suspense=is_suspense,
                is_clearing=is_clearing,
            )

    return make


@pytest_asyncio.fixture
async def usd_accounts(
    account_factory: Callable[..., Awaitable[AccountSnapshot]],
) -> tuple[AccountSnapshot, AccountSnapshot]:
    """A convenience (cash: asset, revenue: revenue) pair, both zero balance,
    both USD."""
    from ledger.models.enums import AccountType

    cash = await account_factory(name="Cash", type=AccountType.ASSET, currency="USD")
    revenue = await account_factory(name="Revenue", type=AccountType.REVENUE, currency="USD")
    return cash, revenue
