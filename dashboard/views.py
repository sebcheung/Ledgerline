"""Dashboard HTML routes (SPEC.md §12 Phase 6): the full page, the
per-panel fragment endpoints (a no-SSE degradation path, and what
`dashboard/sse.py` renders from), the SSE stream, and the demo trigger.

Deliberately thin: every query lives in `ledger.readmodels`
(`dashboard/data.py` composes one context dict per panel), and every
rendering decision lives in the templates.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from dashboard.data import PANEL_LOADERS
from dashboard.demo import run_demo
from dashboard.sse import StreamConfig, event_stream
from dashboard.templating import templates
from ledger.api.deps import SessionDep
from ledger.config import get_settings
from ledger.db.engine import engine as _ledger_engine
from ledger.db.session import async_session_factory

router = APIRouter()


def get_stream_config() -> StreamConfig:
    """A FastAPI dependency, not a query parameter -- reuses the same
    `dependency_overrides` mechanism the test suite already relies on
    (`tests/conftest.py`'s `app_client`), so a test can bound the stream to
    a fixed number of ticks instead of abandoning a live generator. See
    docs/DECISIONS.md."""
    settings = get_settings()
    return StreamConfig(
        interval_seconds=settings.dashboard_sse_interval_seconds,
        keepalive_seconds=settings.dashboard_sse_keepalive_seconds,
        max_stream_seconds=settings.dashboard_sse_max_stream_seconds,
    )


def get_demo_engine() -> AsyncEngine:
    """A dependency for the same reason `get_dashboard_session_factory`
    exists: `run_demo` needs a real `AsyncEngine` (for
    `ledger.reconciliation.runner.execute_run`'s failure-audit path and for
    `Dispatcher`'s `engine.begin()` steps), and importing the module-level
    production `engine` directly would touch it from every environment
    including tests -- see `get_dashboard_session_factory`'s docstring."""
    return _ledger_engine


def get_dashboard_session_factory() -> async_sessionmaker[AsyncSession]:
    """Also a dependency rather than an import `dashboard/sse.py` reaches
    for directly: `event_stream` opens a session itself, every tick, so it
    cannot go through `SessionDep`. Without this indirection it would use
    the module-level `ledger.db.session.async_session_factory` -- bound to
    the *global* production engine -- directly in every environment
    including tests, where every other route's DB access instead goes
    through `get_session`'s `app.dependency_overrides`. `tests/conftest.py`
    documents why that global engine is unsafe to touch from a test at all:
    its pooled asyncpg connections are bound to whichever event loop first
    creates them, and pytest-asyncio hands each test function its own
    loop."""
    return async_session_factory


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: SessionDep) -> HTMLResponse:
    context: dict[str, object] = {"demo_enabled": get_settings().demo_enabled}
    for loader in PANEL_LOADERS.values():
        context.update(await loader(session))
    return templates.TemplateResponse(request=request, name="index.html", context=context)


@router.get("/fragments/{panel}", response_class=HTMLResponse, include_in_schema=False)
async def fragment(panel: str, request: Request, session: SessionDep) -> HTMLResponse:
    loader = PANEL_LOADERS.get(panel)
    if loader is None:
        raise HTTPException(status_code=404, detail=f"no such dashboard panel: {panel!r}")
    context = await loader(session)
    return templates.TemplateResponse(
        request=request, name=f"partials/_{panel}.html", context=context
    )


@router.get("/sse", include_in_schema=False)
async def sse(
    request: Request,
    config: Annotated[StreamConfig, Depends(get_stream_config)],
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_dashboard_session_factory)
    ],
) -> StreamingResponse:
    # Deliberately not `SessionDep`: that would pin one pooled connection
    # idle-in-transaction for the entire life of the browser tab.
    # `event_stream` opens and closes its own session every tick instead.
    return StreamingResponse(
        event_stream(request, session_factory=session_factory, config=config),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo(
    request: Request,
    session: SessionDep,
    engine: Annotated[AsyncEngine, Depends(get_demo_engine)],
) -> HTMLResponse:
    # 404, not 403: a disabled feature shouldn't advertise that it exists at
    # all. Checked here rather than by conditionally registering the route
    # in create_app() -- `get_settings()` is already the single source of
    # truth this whole module reads from, so there is no separate "is the
    # route mounted" state to keep in sync with it.
    if not get_settings().demo_enabled:
        raise HTTPException(status_code=404, detail="the demo scenario is disabled")

    result = await run_demo(session, engine)
    if not result.ran:
        context: dict[str, object] = {
            "ok": False,
            "message": "A demo scenario is already running -- try again shortly.",
        }
    else:
        context = {
            "ok": True,
            "message": (
                f"Demo scenario complete: {result.transactions_posted} transactions, "
                f"{result.lines_ingested} settlement lines, "
                f"{result.deliveries_dead} dead deliveries, "
                f"{result.deliveries_pending} pending retries."
            ),
        }
    return templates.TemplateResponse(request=request, name="partials/_toast.html", context=context)
