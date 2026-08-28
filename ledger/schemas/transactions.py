import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ledger.models.enums import EntryDirection, TransactionSource, TransactionStatus
from ledger.schemas.accounts import EntryRead
from ledger.schemas.common import CurrencyCode, MinorUnits


class EntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID
    direction: EntryDirection
    amount: MinorUnits
    currency: CurrencyCode


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # min_length=2: SPEC.md §5 step 1. max_length=1000 is a lock-count DoS
    # guard -- each distinct account in the entry list is a lock target.
    entries: Annotated[list[EntryCreate], Field(min_length=2, max_length=1000)]
    description: str | None = Field(default=None, max_length=1000)
    external_ref: str | None = Field(default=None, max_length=255)


class TransactionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    idempotency_key: str | None
    external_ref: str | None
    description: str | None
    status: TransactionStatus
    reversal_of: uuid.UUID | None
    source: TransactionSource
    created_at: datetime
    currency: CurrencyCode
    entries: list[EntryRead]


class TransactionSummary(BaseModel):
    """`GET /v1/transactions` list rows -- no entries, one query per page."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    external_ref: str | None
    description: str | None
    status: TransactionStatus
    reversal_of: uuid.UUID | None
    source: TransactionSource
    created_at: datetime


class TransactionListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_ref: str | None = None
    status: TransactionStatus | None = None
    #: Inclusive lower bound.
    created_after: datetime | None = None
    #: Exclusive upper bound (half-open range) -- deliberately not inclusive
    #: on both ends, which is the classic double-count bug in ledger
    #: date-range filters.
    created_before: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @field_validator("created_after", "created_before")
    @classmethod
    def _require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (SPEC.md §13: all timestamps UTC)")
        return value
