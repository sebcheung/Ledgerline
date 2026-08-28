"""Shared FastAPI dependencies.

No auth dependency here yet -- API key auth is Phase 7 (SPEC.md §12), and a
stub would only need to be thrown away.
"""

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
