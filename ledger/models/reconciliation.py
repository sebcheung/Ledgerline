import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, UUIDPKMixin
from ledger.models.enums import (
    ReconciliationFindingType,
    ReconciliationResolution,
    ReconciliationRunStatus,
    pg_enum,
)


class ReconciliationRun(Base, UUIDPKMixin):
    __tablename__ = "reconciliation_runs"

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReconciliationRunStatus] = mapped_column(
        pg_enum(ReconciliationRunStatus, "reconciliation_run_status"),
        nullable=False,
        server_default=ReconciliationRunStatus.RUNNING.value,
    )
    findings_by_type: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ReconciliationFinding(Base, UUIDPKMixin):
    __tablename__ = "reconciliation_findings"
    __table_args__ = (
        # Serves GET /v1/reconciliation/runs/{id}/findings and its keyset
        # cursor -- same (created_at, id) convention as every other
        # paginated listing (see ledger/schemas/pagination.py).
        Index("ix_reconciliation_findings_run_id", "run_id", "created_at", "id"),
        # Re-run idempotency (SPEC.md §10 "run twice, no new findings"):
        # deliberately UNCONDITIONAL, not scoped to resolution='unresolved'
        # -- a resolution-scoped predicate would exclude exactly the rows
        # that most need suppressing on re-run (a suppressed in_flight, or
        # an auto_resolved unexpected_settlement, would fall outside the
        # index and get re-inserted and, in the second case, re-adjusted --
        # a real double-post that verify_global_balance cannot catch,
        # because the ledger stays internally balanced). NULLS NOT DISTINCT
        # is load-bearing: missing_settlement has a NULL
        # settlement_line_id, unexpected_settlement has a NULL
        # transaction_id, and a plain unique index never fires on either.
        # See docs/DECISIONS.md Phase 4.
        Index(
            "uq_recon_findings_open",
            "finding_type",
            "transaction_id",
            "settlement_line_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finding_type: Mapped[ReconciliationFindingType] = mapped_column(
        pg_enum(ReconciliationFindingType, "reconciliation_finding_type"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    settlement_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("settlement_lines.id"), nullable=True
    )
    delta_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resolution: Mapped[ReconciliationResolution] = mapped_column(
        pg_enum(ReconciliationResolution, "reconciliation_resolution"),
        nullable=False,
        server_default=ReconciliationResolution.UNRESOLVED.value,
    )
    resolving_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
