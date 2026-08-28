import pytest
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.health import _alembic_config

pytestmark = pytest.mark.integration


def _current_head() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None
    return head


async def test_readyz_ok_when_migrated(app_client: AsyncClient) -> None:
    response = await app_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_503_when_migration_behind(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    head = _current_head()
    await db_session.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    await db_session.commit()
    try:
        response = await app_client.get("/readyz")
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["reason"] == "migration_pending"
        assert body["head"] == head
    finally:
        await db_session.execute(
            text("UPDATE alembic_version SET version_num = :head"), {"head": head}
        )
        await db_session.commit()
