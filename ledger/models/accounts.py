from sqlalchemy import Boolean, CheckConstraint, Index, String, text
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
        # Phase 4: one clearing account per currency, mirroring is_suspense
        # exactly -- the resolver's `unexpected_settlement`/`amount_mismatch`
        # adjustments need a per-currency "implied asset account" that
        # SPEC.md §7 names but never defines a column for.
        Index(
            "uq_accounts_clearing_per_currency",
            "currency",
            unique=True,
            postgresql_where=text("is_clearing = true"),
        ),
        # A clearing account is an asset account by definition -- cheaper
        # and louder than a runtime check in the resolver.
        CheckConstraint("NOT is_clearing OR type = 'asset'", name="ck_accounts_clearing_is_asset"),
        # One account cannot play both roles: the unexpected_settlement
        # adjustment would debit and credit the same account, and
        # post_transaction would accept that as a balanced, meaningless
        # zero-effect posting.
        CheckConstraint(
            "NOT (is_suspense AND is_clearing)", name="ck_accounts_not_suspense_and_clearing"
        ),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[AccountType] = mapped_column(pg_enum(AccountType, "account_type"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_suspense: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_clearing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
