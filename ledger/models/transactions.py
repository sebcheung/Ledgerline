import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin
from ledger.models.enums import TransactionSource, TransactionStatus, pg_enum


class Transaction(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "transactions"

    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TransactionStatus] = mapped_column(
        pg_enum(TransactionStatus, "transaction_status"),
        nullable=False,
        server_default=TransactionStatus.POSTED.value,
    )
    reversal_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    source: Mapped[TransactionSource] = mapped_column(
        pg_enum(TransactionSource, "transaction_source"), nullable=False
    )
