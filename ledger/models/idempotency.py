from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base
from ledger.models.enums import IdempotencyStatus, pg_enum


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # Phase 7 (SPEC.md §9, docs/DECISIONS.md): serves
        # ledger.core.idempotency.sweep_idempotency_keys's
        # `WHERE status = 'completed' AND created_at < ...` retention
        # DELETE. Deliberately a plain index on created_at, not partial on
        # status='completed' -- every row transitions to 'completed'
        # eventually (see docs/DECISIONS.md), so a partial index would add
        # write-time maintenance for no read-time benefit, and a future
        # "reap abandoned in_progress rows" sweep would want the full index
        # anyway.
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    key: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        pg_enum(IdempotencyStatus, "idempotency_status"), nullable=False
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
