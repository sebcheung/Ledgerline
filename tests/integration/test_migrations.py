import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "accounts",
    "transactions",
    "entries",
    "account_balances",
    "idempotency_keys",
    "outbox_events",
    "webhook_endpoints",
    "webhook_deliveries",
    "settlement_lines",
    "reconciliation_runs",
    "reconciliation_findings",
    "api_keys",
    "alembic_version",
}


@pytest.mark.asyncio
async def test_migration_creates_all_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    assert EXPECTED_TABLES.issubset(set(table_names))
