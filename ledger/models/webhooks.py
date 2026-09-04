import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin
from ledger.models.enums import WebhookDeliveryStatus, pg_enum


class WebhookEndpoint(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class WebhookDelivery(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id", name="uq_webhook_deliveries_event_endpoint"),
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
        # Serves GET /v1/webhooks/deliveries?endpoint_id=... plus its
        # (created_at, id) cursor order in one index, and gives endpoint_id's
        # FK an index it would otherwise lack. No index on `status` alone --
        # four enum values, a heap filter wins at that selectivity (see
        # docs/DECISIONS.md's existing argument for transactions.status).
        Index("ix_webhook_deliveries_endpoint_id", "endpoint_id", "created_at", "id"),
        Index("ix_webhook_deliveries_created_at", "created_at", "id"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_events.id"), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: How many times `sweep_stale_claims` has reclaimed this row from a
    #: worker that died mid-delivery. Deliberately separate from
    #: `attempt_count` (which only counts observed receiver outcomes) --
    #: this is what lets a worker that reliably crashes mid-POST eventually
    #: be dead-lettered instead of redelivered forever (see
    #: `Dispatcher.sweep_stale_claims`).
    reclaim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        pg_enum(WebhookDeliveryStatus, "webhook_delivery_status"),
        nullable=False,
        server_default=WebhookDeliveryStatus.PENDING.value,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
