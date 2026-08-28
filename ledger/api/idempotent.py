"""FastAPI wiring for the idempotency protocol (SPEC.md §6, §11).

SPEC.md §11 describes `idempotent.py` as "a FastAPI dependency". This
module is a dependency (`get_idempotent_request`) plus a `run(execute_fn)`
call made from inside the route body -- matching §6's own
`handle(request, endpoint, key, execute_fn)` signature -- because a plain
dependency cannot record the response after the handler runs, so it cannot
make the ledger write and the key's `status='completed'` UPDATE share one
commit.

The dependency itself does no I/O beyond reading the already-buffered
request body and path params: it never touches the database. This is
deliberate and narrower than it needs to be for correctness reasons, not
style ones. FastAPI resolves every dependency in a route's dependant tree
*before* it validates the endpoint's own Pydantic body model (see
`fastapi.dependencies.utils.solve_dependencies`: the sub-dependency loop
runs to completion, and only then does `request_body_to_args` validate
`dependant.body_params`). If claiming the key happened in the dependency,
a request with syntactically valid JSON that fails `TransactionCreate`
validation would still claim it -- and then nothing would ever complete or
release it, because the route body (and therefore `IdempotentRequest.run`)
never executes. Deferring the claim into `run()`, called explicitly from
inside the route handler, means it only happens once FastAPI has already
accepted the request body.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.deps import SessionDep, get_idempotency_key
from ledger.config import get_settings
from ledger.core.errors import DuplicateTransaction
from ledger.core.idempotency import (
    ClaimOutcome,
    IdempotencyConflict,
    canonical_hash,
    claim_key,
    complete_key,
    load_key,
    release_key,
)

logger = logging.getLogger(__name__)

#: Bump if the stored envelope shape ever changes; an unrecognized version
#: found in an old row should be handled explicitly by the reader, not
#: silently misinterpreted.
_ENVELOPE_VERSION = 1

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class IdempotentResult(Generic[T]):
    """What a route's `execute()` callback produces: the response FastAPI
    would otherwise have built directly -- status code, the Pydantic body,
    and any extra headers (`Location`, most often) that a bare
    `response_model` return can't carry."""

    status: int
    body: T
    headers: dict[str, str] = field(default_factory=dict)


class IdempotentReplay(Exception):
    """Raised from `IdempotentRequest.run` to short-circuit a request whose
    `(key, endpoint)` already has a completed response for a matching
    fingerprint. Not a `LedgerError` -- this is not a failure, it's a cache
    hit -- so it's rendered by its own handler
    (`ledger.api.errors._idempotent_replay_handler`), registered alongside
    the others in `register_error_handlers`.
    """

    def __init__(self, *, status: int, envelope: dict[str, Any]) -> None:
        super().__init__(f"idempotent replay ({status})")
        self.status = status
        self.envelope = envelope


def _endpoint_of(request: Request) -> str:
    """`METHOD route-template`, e.g. `POST /v1/transactions/{transaction_id}/reverse`
    -- SPEC.md §6's "endpoint" scope. The concrete request path is folded
    into the fingerprint instead (see `canonical_hash`), not into this
    scope string, so `endpoint` cardinality stays bounded by the number of
    routes."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


def _envelope(result: IdempotentResult[Any]) -> dict[str, Any]:
    """Serialize with `jsonable_encoder`, the same encoder FastAPI uses to
    build the live 201 response, so a replay is byte-for-byte what the
    original response was."""
    return {
        "v": _ENVELOPE_VERSION,
        "body": jsonable_encoder(result.body),
        "headers": dict(result.headers),
    }


@dataclass(slots=True)
class IdempotentRequest:
    """Bound to one request. `key is None` means the client sent no
    `Idempotency-Key` header, in which case `run` just executes and
    commits -- byte-identical to Phase 2's `await session.commit()` in the
    route body."""

    session: AsyncSession
    key: str | None
    endpoint: str
    fingerprint: str | None
    lock_ttl_seconds: int

    async def run(
        self, execute: Callable[[], Awaitable[IdempotentResult[T]]]
    ) -> IdempotentResult[T]:
        """SPEC.md §6's `handle()`, from `EXECUTE:` down; the claim above it
        lives in `ledger.core.idempotency.claim_key`, called here."""
        if self.key is None:
            result = await execute()
            await self.session.commit()
            return result

        assert self.fingerprint is not None  # set together with key, see get_idempotent_request
        claim = await claim_key(
            self.session,
            key=self.key,
            endpoint=self.endpoint,
            fingerprint=self.fingerprint,
            lock_ttl_seconds=self.lock_ttl_seconds,
        )
        if claim.outcome is ClaimOutcome.REPLAY:
            replay = claim.replay
            assert replay is not None
            raise IdempotentReplay(status=replay.status, envelope=replay.body or {})

        try:
            result = await execute()
        except DuplicateTransaction:
            # SPEC.md §6's except branch. The rollback here is load-bearing:
            # post_transaction's SAVEPOINT (posting.py's begin_nested())
            # lets the session survive the unique-violation, but the outer
            # transaction still holds account_balances FOR UPDATE locks
            # from its own step 3 -- roll back before doing anything else.
            await self.session.rollback()
            logger.info("idempotency.duplicate_backstop", extra={"idempotency_key": self.key})
            raise await self._resolve_duplicate() from None
        except Exception:
            await self.session.rollback()
            assert claim.locked_at is not None
            await release_key(self.session, key=self.key, locked_at=claim.locked_at)
            raise

        await complete_key(
            self.session,
            key=self.key,
            response_status=result.status,
            response_body=_envelope(result),
        )
        await self.session.commit()  # ledger write + key completion, one commit
        return result

    async def _resolve_duplicate(self) -> Exception:
        assert self.key is not None
        assert self.fingerprint is not None
        stored = await load_key(self.session, key=self.key, fingerprint=self.fingerprint)
        if stored is not None:
            return IdempotentReplay(status=stored.status, envelope=stored.body or {})
        # Not (yet) completed -- the request that's actually mid-flight
        # hasn't finished. SPEC.md §6: "else: 409". The key is left
        # in_progress; a later retry reclaims it after the TTL, hits this
        # same DuplicateTransaction branch again, and by then finds the
        # winner completed.
        return IdempotencyConflict(
            f"a request with idempotency key {self.key!r} is already in progress",
            idempotency_key=self.key,
        )


async def get_idempotent_request(
    request: Request,
    session: SessionDep,
    key: Annotated[str | None, Depends(get_idempotency_key)],
) -> IdempotentRequest:
    endpoint = _endpoint_of(request)
    fingerprint = None
    if key is not None:
        # Cached by FastAPI's own body parsing -- not re-read from the wire.
        body = await request.body()
        fingerprint = canonical_hash(body, request.path_params)
    return IdempotentRequest(
        session=session,
        key=key,
        endpoint=endpoint,
        fingerprint=fingerprint,
        lock_ttl_seconds=get_settings().idempotency_lock_ttl_seconds,
    )


IdempotencyDep = Annotated[IdempotentRequest, Depends(get_idempotent_request)]
