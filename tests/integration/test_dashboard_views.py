"""Integration tests for the dashboard's HTML routes (SPEC.md §12 Phase 6):
every route renders, empty states render on a fresh database, a fragment
response is never the full page, and errors under `/dashboard` render HTML
while `/v1` keeps its `application/problem+json` contract."""

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

_FRAGMENT_PANELS = ("balances", "transactions", "reconciliation", "webhooks")


async def _create_account(app_client: AsyncClient, **kwargs: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Account",
        "type": "asset",
        "currency": "USD",
        "allow_negative": False,
    }
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_index_returns_html(app_client: AsyncClient) -> None:
    response = await app_client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text


@pytest.mark.parametrize("panel", _FRAGMENT_PANELS)
async def test_fragment_route_returns_200_html(app_client: AsyncClient, panel: str) -> None:
    response = await app_client.get(f"/dashboard/fragments/{panel}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    ("panel", "empty_text"),
    [
        ("balances", "No accounts yet."),
        ("transactions", "No transactions yet."),
        ("reconciliation", "No reconciliation runs yet."),
        ("webhooks", "No deliveries in flight."),
    ],
)
async def test_fragments_render_empty_state_on_a_fresh_database(
    app_client: AsyncClient, panel: str, empty_text: str
) -> None:
    response = await app_client.get(f"/dashboard/fragments/{panel}")
    assert response.status_code == 200
    assert empty_text in response.text


@pytest.mark.parametrize("panel", _FRAGMENT_PANELS)
async def test_fragment_response_is_a_fragment_not_a_full_page(
    app_client: AsyncClient, panel: str
) -> None:
    """Without this assertion, every fragment test above would still pass
    even if a fragment template accidentally `{% extends "base.html" %}`ed
    -- htmx's `innerHTML` swap into a `<section>` would then nest a second
    `<html>` document inside it."""
    response = await app_client.get(f"/dashboard/fragments/{panel}")
    assert "<html" not in response.text
    assert "<body" not in response.text


async def test_unknown_fragment_panel_404s(app_client: AsyncClient) -> None:
    response = await app_client.get("/dashboard/fragments/nope")
    assert response.status_code == 404


async def test_balances_fragment_shows_posted_balance(app_client: AsyncClient) -> None:
    await _create_account(app_client, name="Cash", allow_negative=True)
    response = await app_client.get("/dashboard/fragments/balances")
    assert response.status_code == 200
    assert "Cash" in response.text
    assert "0.00 USD" in response.text


async def test_transactions_fragment_lists_recent_first(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", allow_negative=True)
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    for ref in ("first", "second", "third"):
        response = await app_client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {
                        "account_id": cash["id"],
                        "direction": "debit",
                        "amount": 100,
                        "currency": "USD",
                    },
                    {
                        "account_id": revenue["id"],
                        "direction": "credit",
                        "amount": 100,
                        "currency": "USD",
                    },
                ],
                "external_ref": ref,
            },
        )
        assert response.status_code == 201, response.text

    fragment = await app_client.get("/dashboard/fragments/transactions")
    body = fragment.text
    assert body.index("third") < body.index("second") < body.index("first")


async def test_dashboard_disabled_returns_404(
    monkeypatch: pytest.MonkeyPatch, db_engine: AsyncEngine
) -> None:
    from ledger.config import get_settings
    from ledger.db.session import get_session

    disabled_settings = get_settings().model_copy(update={"dashboard_enabled": False})
    monkeypatch.setattr("ledger.api.main.get_settings", lambda: disabled_settings)

    from ledger.api.main import create_app

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/dashboard")
        assert response.status_code == 404
        # /v1 is unaffected by the flag.
        healthz = await client.get("/healthz")
        assert healthz.status_code == 200


async def test_dashboard_error_renders_html_while_v1_error_renders_problem_json(
    app_client: AsyncClient,
) -> None:
    dashboard_response = await app_client.get("/dashboard/fragments/bogus")
    assert dashboard_response.status_code == 404
    assert dashboard_response.headers["content-type"].startswith("text/html")
    assert "<html" in dashboard_response.text

    v1_response = await app_client.get(f"/v1/accounts/{uuid.uuid4()}")
    assert v1_response.status_code == 404
    assert v1_response.headers["content-type"] == "application/problem+json"


async def test_dashboard_hx_request_error_is_a_200_fragment(app_client: AsyncClient) -> None:
    """htmx does not swap a non-2xx response at all, so an `HX-Request`
    error must come back as 200 carrying an error fragment -- the real
    status is logged server-side, not lost, just not on the wire."""
    response = await app_client.get("/dashboard/fragments/bogus", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "404" in response.text
    assert "<html" not in response.text


async def test_static_asset_is_served(app_client: AsyncClient) -> None:
    response = await app_client.get("/dashboard/static/vendor/htmx.min.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_demo_route_is_disabled_by_default(app_client: AsyncClient) -> None:
    """`demo_enabled` defaults to `False` (`ledger/config.py`) -- a fresh
    `app_client` must never be able to trigger a scenario that writes real
    ledger rows."""
    response = await app_client.post("/dashboard/demo")
    assert response.status_code == 404


async def test_demo_route_runs_the_scenario_when_enabled(
    monkeypatch: pytest.MonkeyPatch, db_engine: AsyncEngine
) -> None:
    """Exercises the demo route's success path end to end over real HTTP --
    `dashboard.demo.run_demo` itself is covered in depth by
    `tests/faults/test_demo_scenario.py`; this only pins that the route
    wires it up correctly and renders the toast."""
    from dashboard.views import get_demo_engine
    from ledger.config import get_settings
    from ledger.db.session import get_session

    enabled_settings = get_settings().model_copy(update={"demo_enabled": True})
    monkeypatch.setattr("dashboard.views.get_settings", lambda: enabled_settings)

    from ledger.api.main import create_app

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app = create_app()
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_demo_engine] = lambda: db_engine

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/dashboard/demo")
        assert response.status_code == 200
        assert "Demo scenario complete" in response.text


def test_dashboard_dependency_helpers_return_the_expected_shapes() -> None:
    """`get_stream_config`, `get_dashboard_session_factory`, and
    `get_demo_engine` are plain FastAPI dependency functions -- every
    integration test overrides them (necessarily: see their docstrings for
    why touching the real production engine/settings from a test is
    unsafe), so nothing else ever calls their actual bodies."""
    from sqlalchemy.ext.asyncio import AsyncEngine as _AsyncEngine

    from dashboard.sse import StreamConfig
    from dashboard.views import get_dashboard_session_factory, get_demo_engine, get_stream_config

    assert isinstance(get_stream_config(), StreamConfig)
    assert isinstance(get_demo_engine(), _AsyncEngine)
    assert get_dashboard_session_factory() is not None
