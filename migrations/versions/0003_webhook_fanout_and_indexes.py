"""webhook fan-out tracking and delivery listing indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01

Phase 5 (SPEC.md §8) needs a way for the dispatcher to find outbox_events
that have not yet been fanned out to webhook_deliveries. `fanned_out_at`
plus a partial index on the unfanned set makes that scan self-truncating --
see docs/DECISIONS.md Phase 5 for why a NOT EXISTS anti-join and a
created_at watermark were both rejected.

This migration does *not* add `ix_webhook_deliveries_status_next_attempt` --
that index already exists, created by 0001_initial_schema.py. The Phase 4
note claiming otherwise (docs/DECISIONS.md, in the Phase 4 section) was
wrong; it is corrected in place and the correction is recorded under
Phase 5.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("fanned_out_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_events_unfanned",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("fanned_out_at IS NULL"),
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_id",
        "webhook_deliveries",
        ["endpoint_id", "created_at", "id"],
    )
    op.create_index(
        "ix_webhook_deliveries_created_at",
        "webhook_deliveries",
        ["created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_created_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_endpoint_id", table_name="webhook_deliveries")
    op.drop_index("ix_outbox_events_unfanned", table_name="outbox_events")
    op.drop_column("outbox_events", "fanned_out_at")
