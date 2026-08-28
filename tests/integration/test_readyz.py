import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_readyz_ok_when_migrated(app_client: AsyncClient) -> None:
    response = await app_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_503_when_migration_behind(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    await db_session.commit()
    try:
        response = await app_client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "migration_pending"
    finally:
        await db_session.execute(text("UPDATE alembic_version SET version_num = '0001'"))
        await db_session.commit()
