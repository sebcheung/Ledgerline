from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ledger.config import get_settings


def create_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True, echo=False)


engine: AsyncEngine = create_engine()
