"""webhook reclaim count

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-03

A worker that reliably crashes mid-POST (e.g. a bad deploy that dies right
after the socket write) leaves its claimed row's `attempt_count` untouched
forever -- `sweep_stale_claims` deliberately never charges an attempt for a
crash it did not observe (see `ledger.webhooks.dispatcher`'s module
docstring). Without a separate counter, that row is redelivered forever
instead of eventually being dead-lettered. `reclaim_count` is that counter:
incremented by `sweep_stale_claims` on every reclaim, independent of
`attempt_count`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_deliveries",
        sa.Column("reclaim_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("webhook_deliveries", "reclaim_count")
