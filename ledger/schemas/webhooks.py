"""Pydantic v2 request/response models for
`ledger/api/routes/webhooks.py` (SPEC.md §8, §9).

Field-shape validation only, per the Phase 2 convention (see
docs/DECISIONS.md): anything that needs a stable RFC 7807 type URI (a
delivery that isn't retryable, an unknown endpoint) is raised as a
`LedgerError`, not enforced here.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ledger.models.enums import WebhookDeliveryStatus


class WebhookEndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    active: bool = True


class WebhookEndpointRead(BaseModel):
    """Deliberately has no `secret` field -- not merely redacted, but
    structurally absent, so a future field added to this model can never
    leak it by accident. See `WebhookEndpointCreated` for the one place the
    secret is ever returned."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    url: str
    active: bool
    created_at: datetime


class WebhookEndpointCreated(WebhookEndpointRead):
    """The response to `POST /v1/webhooks/endpoints` only -- the one and
    only time the secret is ever returned. There is no rotation or reveal
    endpoint; if it's lost, the endpoint must be re-created."""

    secret: str


class WebhookEndpointListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class WebhookDeliveryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: WebhookDeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    last_response_code: int | None
    claimed_at: datetime | None
    created_at: datetime


class WebhookDeliveryListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WebhookDeliveryStatus | None = None
    endpoint_id: uuid.UUID | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
