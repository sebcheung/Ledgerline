import pytest
from sqlalchemy import inspect, text
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


@pytest.mark.asyncio
async def test_unfanned_outbox_index_predicate_matches_the_model(db_engine: AsyncEngine) -> None:
    """Alembic's `compare_indexes` does not compare `postgresql_where`
    (docs/DECISIONS.md), so the model's partial predicate on
    `ix_outbox_events_unfanned` can drift from the migration's without
    `alembic check` ever noticing. Pin the actual index definition
    directly against `pg_indexes` instead."""
    async with db_engine.connect() as conn:
        indexdef = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": "ix_outbox_events_unfanned"},
            )
        ).scalar_one()
    assert "fanned_out_at IS NULL" in indexdef
