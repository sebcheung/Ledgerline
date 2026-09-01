"""Pydantic v2 request/response models for
`ledger/api/routes/reconciliation.py` (SPEC.md §7, §9)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ledger.models.enums import ReconciliationFindingType, ReconciliationResolution


class ReconciliationRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    window_start: datetime
    window_end: datetime
    cutoff_at: datetime
    status: str
    #: {"observed": {finding_type: count}, "created": {finding_type: count}}
    #: -- two counts, not one: on a re-run over an unchanged window,
    #: `created` legitimately reads all-zero while `observed` still shows
    #: the open drift (SPEC.md §10's "no new findings" is about `created`).
    findings_by_type: dict[str, Any] | None


class ReconciliationFindingRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    finding_type: ReconciliationFindingType
    transaction_id: uuid.UUID | None
    settlement_line_id: uuid.UUID | None
    delta_amount: int | None
    detail: dict[str, Any] | None
    resolution: ReconciliationResolution
    resolving_transaction_id: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


class ReconciliationFindingListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: ReconciliationResolution | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class FindingResolveAction(StrEnum):
    POST_ADJUSTMENT = "post_adjustment"
    SUPPRESS = "suppress"


class FindingResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: FindingResolveAction
    note: str | None = Field(default=None, max_length=1000)
