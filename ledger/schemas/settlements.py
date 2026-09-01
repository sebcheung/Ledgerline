"""Pydantic v2 request/response models for `ledger/api/routes/settlements.py`
(SPEC.md §7, §9)."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ledger.schemas.common import CurrencyCode, SignedMinorUnits

#: Deliberately SignedMinorUnits, not MinorUnits: unlike `entries.amount`
#: (SPEC.md §3 `CHECK (amount > 0)`), `settlement_lines.amount` carries no
#: such constraint. A real feed contains refunds (negative amounts). This
#: is exactly why the resolver's adjustments sign-swap on amount/delta
#: rather than assuming positive.
SettlementAmount = SignedMinorUnits


class SettlementLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_ref: str | None = Field(default=None, max_length=255)
    amount: SettlementAmount
    currency: CurrencyCode
    value_date: date
    #: The original line, preserved verbatim in `settlement_lines.raw`.
    raw: dict[str, Any] = Field(default_factory=dict)


class SettlementIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[SettlementLineIn] = Field(min_length=1, max_length=10_000)


class SettlementIngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    ingested: int
    deduplicated: int


class SettlementLineRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    external_ref: str | None
    amount: SettlementAmount
    currency: CurrencyCode
    value_date: date
    raw: dict[str, Any]
    batch_id: uuid.UUID
    ingested_at: datetime
    matched_transaction_id: uuid.UUID | None


class SettlementListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID | None = None
    #: True: matched_transaction_id IS NOT NULL. False: IS NULL. Omitted:
    #: no filter.
    matched: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
