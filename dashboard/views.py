"""Dashboard HTML routes (SPEC.md §12 Phase 6): the full page, the
per-panel fragment endpoints (a no-SSE degradation path, and what
`dashboard/sse.py` renders from), and -- once `feature/demo-scenario` lands
-- the demo trigger.

Deliberately thin: every query lives in `ledger.readmodels`
(`dashboard/data.py` composes one context dict per panel), and every
rendering decision lives in the templates.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.data import PANEL_LOADERS
from dashboard.templating import templates
from ledger.api.deps import SessionDep
from ledger.config import get_settings

router = APIRouter()


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
