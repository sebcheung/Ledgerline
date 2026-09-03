"""idempotency key retention index

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-03

Phase 7 (SPEC.md §9, §12): `idempotency_keys` grows without bound -- one
row per idempotent request, forever, with no expiry. docs/DECISIONS.md
(Phase 3) deferred both this index and the retention sweep that uses it
("`ledger.core.idempotency.sweep_idempotency_keys`") to Phase 7, once
there was an operational surface (`ledger.admin.sweep`, alongside
`/metrics` and deploy) to run it from.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
