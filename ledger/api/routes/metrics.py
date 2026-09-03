"""`GET /metrics` (SPEC.md §9 Phase 7): Prometheus text exposition format.

Mounted with no prefix and no `V1_DEPENDENCIES` (see `ledger/api/main.py`)
-- deliberately unauthenticated, because Fly's built-in Prometheus scraper
polls over the private network and cannot send an `Authorization` header
(see `docs/DECISIONS.md` Phase 7). Gated behind `metrics_enabled` for
anyone who wants the endpoint gone entirely.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ledger.api.deps import SessionDep
from ledger.config import get_settings
from ledger.observability.metrics import REGISTRY, refresh_db_gauges

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(session: SessionDep) -> PlainTextResponse:
    if not get_settings().metrics_enabled:
        raise HTTPException(status_code=404)
    await refresh_db_gauges(session)
    return PlainTextResponse(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
