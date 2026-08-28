import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    )

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_events.id"), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        pg_enum(WebhookDeliveryStatus, "webhook_delivery_status"),
        nullable=False,
        server_default=WebhookDeliveryStatus.PENDING.value,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
