"""Shared DB test helpers: truncation-based isolation and small typed readers.

`text()` results are typed `Any` by SQLAlchemy; mypy --strict is configured to
cover `tests/` too, so every raw-SQL result gets cast through one narrow helper
here rather than scattering `# type: ignore` across the suite.
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Deliberately excludes `alembic_version` -- truncating it would desynchronize
# the migration-head check that `/readyz` and the session-scoped migration
# fixture rely on. Order does not matter: TRUNCATE lists everything in one
# statement, which Postgres resolves as a single atomic operation regardless
# of FK direction.
LEDGER_TABLES: tuple[str, ...] = (
    "reconciliation_findings",
    "reconciliation_runs",
    "settlement_lines",
    "webhook_deliveries",
    "webhook_endpoints",
    "outbox_events",
    "idempotency_keys",
    "account_balances",
    "entries",
    "transactions",
    "accounts",
    "api_keys",
)


async def truncate_all(engine: AsyncEngine) -> None:
    """Reset every ledger table to empty in one atomic statement.

    Uses TRUNCATE rather than DELETE because `entries` carries a row-level
    `BEFORE UPDATE OR DELETE` trigger (`entries_no_update`) that rejects
    DELETE outright; TRUNCATE is neither UPDATE nor DELETE and is not
    intercepted by a row-level trigger, so it is the only statement that can
    clear an append-only table between tests. `lock_timeout` turns a leaked
    open transaction from a previous test into a fast, legible failure
    instead of a hung CI job.
    """
    stmt = text(f"TRUNCATE {', '.join(LEDGER_TABLES)} RESTART IDENTITY CASCADE")
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        await conn.execute(stmt)


async def scalar_int(conn_execute_result: Any) -> int:
    """Narrow a `Result` from a `text()` statement to `int` for mypy --strict."""
    return cast(int, conn_execute_result.scalar_one())


def row_mapping(row: Any) -> Mapping[str, Any]:
    """Narrow a `Row` from a `text()` statement to a mapping for mypy --strict."""
    return cast(Mapping[str, Any], row._mapping)


def row_mappings(rows: Sequence[Any]) -> list[Mapping[str, Any]]:
    return [row_mapping(r) for r in rows]
