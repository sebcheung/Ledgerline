"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.auth import require_api_key
from ledger.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Phase 7 (SPEC.md §9): attached to every `/v1` router's `include_router(...,
#: dependencies=V1_DEPENDENCIES)` call in `ledger/api/main.py`. Deliberately
#: *not* applied via middleware or an allowlist -- see `ledger/api/auth.py`'s
#: module docstring -- so `/healthz`, `/readyz`, `/metrics`, and
#: `/dashboard/*` are unauthenticated by construction, not by exception.
V1_DEPENDENCIES = [Depends(require_api_key)]


def get_idempotency_key(
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ] = None,
) -> str | None:
    """Read the client-supplied idempotency key, if any.

    Optional -- a request with no key executes directly, protected only by
    the `transactions.idempotency_key` unique constraint backstop (SPEC.md
    §5 step 6), same as Phase 2. Capped at 255 chars: `idempotency_keys.key`
    is this column's primary key, and an unbounded header is a cheap way to
    bloat that index; an over-long key 422s here as ordinary header
    validation, before any dependency that would touch the database runs.

    `ledger.api.idempotent.get_idempotent_request` wraps this key with the
    fingerprint and claim/replay/reclaim protocol from SPEC.md §6.
    """
    return idempotency_key
