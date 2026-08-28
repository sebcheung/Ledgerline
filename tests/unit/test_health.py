import pytest
from httpx import ASGITransport, AsyncClient

from ledger.api.main import create_app


@pytest.mark.asyncio
async def test_healthz_returns_ok_without_db() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
