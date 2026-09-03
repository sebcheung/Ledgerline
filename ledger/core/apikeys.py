"""API key issuance and lookup (SPEC.md §9 Phase 7).

Framework-free, like the rest of `ledger.core` -- `ledger.api.auth` wraps
`lookup_active_key` with the FastAPI dependency, the TTL cache, and the
`Unauthenticated` mapping. Kept separate so the admin CLI
(`ledger.admin.keys`) can mint and revoke keys without importing FastAPI.

Only a SHA-256 hash of the raw key is ever stored (`api_keys.key_hash`,
unique) -- the raw value is generated once, printed once, and is not
recoverable. A hash lookup on a unique btree index needs no constant-time
comparison: the database is comparing hashes, not the caller's secret
against a value it echoes back, so there is nothing for a timing side
channel to leak about the preimage.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.api_keys import ApiKey

#: `secrets.token_urlsafe(32)` yields 256 bits of entropy in 43 base64url
#: characters; prefixed so a key is recognizable at a glance (and greppable
#: out of logs) the way Stripe/GitHub-style tokens are.
_KEY_PREFIX = "lk_"


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    """The subset of an `ApiKey` row a request needs after authentication.
    Not the ORM instance -- callers must not be tempted to hold it across
    a session boundary (see `AccountSnapshot`'s docstring for the same
    reasoning)."""

    id: uuid.UUID
    name: str


def generate_api_key() -> str:
    """A new raw API key. Never stored -- only `hash_api_key(...)` of it is."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def create_api_key(session: AsyncSession, *, name: str) -> tuple[uuid.UUID, str]:
    """Insert a new active key and return `(id, raw_key)`. The caller must
    display `raw_key` to the operator immediately -- it cannot be recovered
    once this returns, since only its hash is persisted."""
    raw_key = generate_api_key()
    row = (
        await session.execute(
            insert(ApiKey).values(key_hash=hash_api_key(raw_key), name=name).returning(ApiKey.id)
        )
    ).one()
    return row.id, raw_key


async def lookup_active_key(session: AsyncSession, key_hash: str) -> AuthenticatedKey | None:
    row = (
        await session.execute(
            select(ApiKey.id, ApiKey.name).where(
                ApiKey.key_hash == key_hash, ApiKey.active.is_(True)
            )
        )
    ).first()
    if row is None:
        return None
    return AuthenticatedKey(id=row.id, name=row.name)


async def list_api_keys(session: AsyncSession) -> list[ApiKey]:
    rows = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars().all()
    return list(rows)


async def revoke_api_key(session: AsyncSession, *, key_id: uuid.UUID) -> bool:
    """Flip `active` to false. Returns whether a row matched -- the caller
    can distinguish "revoked" from "no such key" without a second query."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id, ApiKey.active.is_(True)).values(active=False)
        ),
    )
    return result.rowcount > 0
