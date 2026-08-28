import os
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def app_client(
    migrated_database_url: str, db_engine: AsyncEngine
) -> AsyncGenerator[AsyncClient, None]:
    from ledger.api.main import create_app
    from ledger.db.session import get_session

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
