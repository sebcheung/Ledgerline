from sqlalchemy import Boolean, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin
from ledger.models.enums import AccountType, pg_enum


class Account(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "accounts"
    __table_args__ = (
        Index("ix_accounts_currency", "currency"),
        # One suspense account per currency (SPEC.md §3).
        Index(
            "uq_accounts_suspense_per_currency",
            "currency",
            unique=True,
            postgresql_where=text("is_suspense = true"),
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[AccountType] = mapped_column(pg_enum(AccountType, "account_type"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_suspense: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
