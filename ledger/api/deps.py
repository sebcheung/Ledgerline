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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Read the client-supplied idempotency key, if any.

    Phase 2 passes this straight through to `post_transaction`/
    `reverse_transaction`, so the `transactions.idempotency_key` unique
    constraint backstop (SPEC.md §5 step 6) is live even before Phase 3's
    full protocol (claim/replay/stale-lock reclamation) exists. Phase 3
    replaces this dependency with one that implements that full protocol,
    without changing the service-layer signature.
    """
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
