"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_TYPE = postgresql.ENUM(
    "asset", "liability", "equity", "revenue", "expense",
    name="account_type", create_type=False,
)
TRANSACTION_STATUS = postgresql.ENUM(
    "posted", "reversed", name="transaction_status", create_type=False,
)
TRANSACTION_SOURCE = postgresql.ENUM(
    "api", "reconciliation", name="transaction_source", create_type=False,
)
ENTRY_DIRECTION = postgresql.ENUM(
    "debit", "credit", name="entry_direction", create_type=False,
)
IDEMPOTENCY_STATUS = postgresql.ENUM(
    "in_progress", "completed", name="idempotency_status", create_type=False,
)
WEBHOOK_DELIVERY_STATUS = postgresql.ENUM(
    "pending", "delivering", "succeeded", "dead",
    name="webhook_delivery_status", create_type=False,
)
RECONCILIATION_RUN_STATUS = postgresql.ENUM(
    "running", "completed", "failed", name="reconciliation_run_status", create_type=False,
)
RECONCILIATION_FINDING_TYPE = postgresql.ENUM(
    "in_flight", "missing_settlement", "unexpected_settlement",
    "amount_mismatch", "currency_mismatch", "duplicate_settlement",
    name="reconciliation_finding_type", create_type=False,
)
RECONCILIATION_RESOLUTION = postgresql.ENUM(
    "unresolved", "auto_resolved", "manually_resolved", "suppressed",
    name="reconciliation_resolution", create_type=False,
)

ALL_ENUMS = (
    ACCOUNT_TYPE,
    TRANSACTION_STATUS,
    TRANSACTION_SOURCE,
    ENTRY_DIRECTION,
    IDEMPOTENCY_STATUS,
    WEBHOOK_DELIVERY_STATUS,
    RECONCILIATION_RUN_STATUS,
    RECONCILIATION_FINDING_TYPE,
    RECONCILIATION_RESOLUTION,
)

APPEND_ONLY_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION reject_entry_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'entries is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGER_SQL = """
CREATE TRIGGER entries_no_update BEFORE UPDATE OR DELETE ON entries
  FOR EACH ROW EXECUTE FUNCTION reject_entry_mutation();
"""

DROP_APPEND_ONLY_TRIGGER_SQL = "DROP TRIGGER IF EXISTS entries_no_update ON entries;"

DROP_APPEND_ONLY_TRIGGER_FUNCTION_SQL = "DROP FUNCTION IF EXISTS reject_entry_mutation();"


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", ACCOUNT_TYPE, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("allow_negative", sa.Boolean(), nullable=False),
        sa.Column("is_suspense", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_accounts_currency", "accounts", ["currency"])
    op.create_index(
        "uq_accounts_suspense_per_currency",
        "accounts",
        ["currency"],
        unique=True,
        postgresql_where=sa.text("is_suspense = true"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("idempotency_key", sa.String(), nullable=True, unique=True),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", TRANSACTION_STATUS, nullable=False,
                  server_default=sa.text("'posted'")),
        sa.Column("reversal_of", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("source", TRANSACTION_SOURCE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index("ix_transactions_external_ref", "transactions", ["external_ref"])
    op.create_index(
        "uq_transactions_reversal_of",
        "transactions",
        ["reversal_of"],
        unique=True,
        postgresql_where=sa.text("reversal_of IS NOT NULL"),
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("transaction_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("transactions.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("direction", ENTRY_DIRECTION, nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("amount > 0", name="ck_entries_amount_positive"),
    )
    op.create_index("ix_entries_account_id_created_at", "entries", ["account_id", "created_at"])

    op.create_table(
        "account_balances",
        sa.Column("account_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("entry_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("status", IDEMPOTENCY_STATUS, nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("outbox_events.id"), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("webhook_endpoints.id"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", WEBHOOK_DELIVERY_STATUS, nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_response_code", sa.Integer(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "endpoint_id",
                             name="uq_webhook_deliveries_event_endpoint"),
    )
    op.create_index(
        "ix_webhook_deliveries_status_next_attempt",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "settlement_lines",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("batch_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("matched_transaction_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("transactions.id"), nullable=True),
    )

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", RECONCILIATION_RUN_STATUS, nullable=False,
                  server_default=sa.text("'running'")),
        sa.Column("findings_by_type", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "reconciliation_findings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("reconciliation_runs.id"), nullable=False),
        sa.Column("finding_type", RECONCILIATION_FINDING_TYPE, nullable=False),
        sa.Column("transaction_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("settlement_line_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("settlement_lines.id"), nullable=True),
        sa.Column("delta_amount", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("resolution", RECONCILIATION_RESOLUTION, nullable=False,
                  server_default=sa.text("'unresolved'")),
        sa.Column("resolving_transaction_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("key_hash", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.execute(APPEND_ONLY_TRIGGER_FUNCTION_SQL)
    op.execute(APPEND_ONLY_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(DROP_APPEND_ONLY_TRIGGER_SQL)
    op.execute(DROP_APPEND_ONLY_TRIGGER_FUNCTION_SQL)

    op.drop_table("api_keys")
    op.drop_table("reconciliation_findings")
    op.drop_table("reconciliation_runs")
    op.drop_table("settlement_lines")
    op.drop_index("ix_webhook_deliveries_status_next_attempt", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
    op.drop_table("outbox_events")
    op.drop_table("idempotency_keys")
    op.drop_table("account_balances")
    op.drop_index("ix_entries_account_id_created_at", table_name="entries")
    op.drop_table("entries")
    op.drop_index("uq_transactions_reversal_of", table_name="transactions")
    op.drop_index("ix_transactions_external_ref", table_name="transactions")
    op.drop_index("ix_transactions_created_at", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("uq_accounts_suspense_per_currency", table_name="accounts")
    op.drop_index("ix_accounts_currency", table_name="accounts")
    op.drop_table("accounts")

    bind = op.get_bind()
    for enum in reversed(ALL_ENUMS):
        enum.drop(bind, checkfirst=True)
