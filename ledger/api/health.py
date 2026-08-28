from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.session import get_session

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
        current = result.scalar_one_or_none()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail={"status": "database_unreachable", "error": str(exc)}
        ) from exc

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if current != head:
        raise HTTPException(
            status_code=503,
            detail={"status": "migration_pending", "current": current, "head": head},
        )
    return {"status": "ok"}
