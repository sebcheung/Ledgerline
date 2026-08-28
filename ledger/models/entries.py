import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin
from ledger.models.enums import EntryDirection, pg_enum


class Entry(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_entries_amount_positive"),
        Index("ix_entries_account_id_created_at", "account_id", "created_at"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    direction: Mapped[EntryDirection] = mapped_column(
        pg_enum(EntryDirection, "entry_direction"), nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
