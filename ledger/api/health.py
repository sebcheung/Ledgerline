import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


@router.get("/healthz", tags=["health"], summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/readyz",
    tags=["health"],
    summary="Readiness probe: database connectivity and Alembic head check",
)
async def readyz(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    # HTTPException.detail keys use `reason`, not `status` -- the RFC 7807
    # problem document reserves `status` for the integer HTTP status code,
    # and `_http_exception_handler` merges this dict's keys in as top-level
    # extension members.
    try:
        await session.execute(text("SELECT 1"))
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        current = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("readyz database check failed", exc_info=exc)
        raise HTTPException(status_code=503, detail={"reason": "database_unreachable"}) from exc

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if current != head:
        raise HTTPException(
            status_code=503,
            detail={"reason": "migration_pending", "current": current, "head": head},
        )
    return {"status": "ok"}
