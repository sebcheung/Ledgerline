from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin
from ledger.models.enums import AccountType, pg_enum


class Account(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "accounts"

    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[AccountType] = mapped_column(pg_enum(AccountType, "account_type"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_suspense: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
