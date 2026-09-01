from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin


class OutboxEvent(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (
        # Partial: the dispatcher's fan-out step scans exactly this set
        # (SPEC.md §8 "Fan-out"), and the predicate keeps the index small
        # forever regardless of how large outbox_events grows -- a row
        # leaves the index the moment it's fanned out. See docs/DECISIONS.md
        # Phase 5 for the alternatives this was chosen over.
        Index(
            "ix_outbox_events_unfanned",
            "created_at",
            postgresql_where=text("fanned_out_at IS NULL"),
        ),
    )

    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fanned_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
