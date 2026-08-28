from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Native Postgres enum column type. The Alembic migration owns CREATE TYPE,
    so create_type=False prevents SQLAlchemy from trying to create it itself."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class TransactionStatus(StrEnum):
    POSTED = "posted"
    REVERSED = "reversed"


class TransactionSource(StrEnum):
    API = "api"
    RECONCILIATION = "reconciliation"


class EntryDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class ReconciliationRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReconciliationFindingType(StrEnum):
    IN_FLIGHT = "in_flight"
    MISSING_SETTLEMENT = "missing_settlement"
    UNEXPECTED_SETTLEMENT = "unexpected_settlement"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DUPLICATE_SETTLEMENT = "duplicate_settlement"


class ReconciliationResolution(StrEnum):
    UNRESOLVED = "unresolved"
    AUTO_RESOLVED = "auto_resolved"
    MANUALLY_RESOLVED = "manually_resolved"
    SUPPRESSED = "suppressed"
