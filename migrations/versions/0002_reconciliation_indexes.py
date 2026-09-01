"""reconciliation indexes and accounts.is_clearing

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-28

Phase 4 (SPEC.md §7) needs indexes `0001` never created:
`settlement_lines` and `reconciliation_findings` shipped with none at all,
and the matcher/findings-API both scan them. This is the first migration
Phase 2/3 didn't need -- see docs/DECISIONS.md.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("is_clearing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "uq_accounts_clearing_per_currency",
        "accounts",
        ["currency"],
        unique=True,
        postgresql_where=sa.text("is_clearing = true"),
    )
    op.create_check_constraint(
        "ck_accounts_clearing_is_asset", "accounts", "NOT is_clearing OR type = 'asset'"
    )
    op.create_check_constraint(
        "ck_accounts_not_suspense_and_clearing",
        "accounts",
        "NOT (is_suspense AND is_clearing)",
    )

    op.create_index(
        "ix_settlement_lines_unmatched",
        "settlement_lines",
        ["value_date"],
        postgresql_where=sa.text("matched_transaction_id IS NULL"),
    )
    op.create_index(
        "ix_settlement_lines_matched_transaction_id",
        "settlement_lines",
        ["matched_transaction_id"],
    )
    op.create_index("ix_settlement_lines_batch_id", "settlement_lines", ["batch_id"])

    op.add_column(
        "reconciliation_findings",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_reconciliation_findings_run_id",
        "reconciliation_findings",
        ["run_id", "created_at", "id"],
    )
    op.create_index(
        "uq_recon_findings_open",
        "reconciliation_findings",
        ["finding_type", "transaction_id", "settlement_line_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_recon_findings_open", table_name="reconciliation_findings")
    op.drop_index("ix_reconciliation_findings_run_id", table_name="reconciliation_findings")
    op.drop_column("reconciliation_findings", "created_at")

    op.drop_index("ix_settlement_lines_batch_id", table_name="settlement_lines")
    op.drop_index("ix_settlement_lines_matched_transaction_id", table_name="settlement_lines")
    op.drop_index("ix_settlement_lines_unmatched", table_name="settlement_lines")

    op.drop_constraint("ck_accounts_not_suspense_and_clearing", "accounts", type_="check")
    op.drop_constraint("ck_accounts_clearing_is_asset", "accounts", type_="check")
    op.drop_index("uq_accounts_clearing_per_currency", table_name="accounts")
    op.drop_column("accounts", "is_clearing")
