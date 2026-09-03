"""Bearer API key authentication (SPEC.md §9 Phase 7).

Applied at router-mount time (`ledger/api/main.py`'s `V1_DEPENDENCIES`, via
`ledger/api/deps.py`), not per-route and not as ASGI middleware -- see
`docs/DECISIONS.md` Phase 7. Router-level `dependencies=` makes the
exclusion of `/healthz`, `/readyz`, `/metrics`, and `/dashboard/*`
structural (those routers simply never receive this dependency) rather
than a path allowlist that a new route could silently fall outside of.

Using `fastapi.security.HTTPBearer` rather than reading the `Authorization`
header by hand is what makes FastAPI emit an OpenAPI `securitySchemes`
entry and a `security` requirement on every `/v1` operation for free.
`auto_error=False` so a missing/malformed header raises our own
`Unauthenticated` (RFC 7807) instead of `HTTPBearer`'s bare `HTTPException`.
"""

import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.apikeys import AuthenticatedKey, hash_api_key, lookup_active_key
from ledger.core.errors import Unauthenticated
from ledger.db.session import get_session

#: A local `Annotated` alias rather than importing `ledger.api.deps.SessionDep`
#: -- `deps.py` assembles `V1_DEPENDENCIES` from `require_api_key` below, so
#: importing the other way would be circular.
_SessionDep = Annotated[AsyncSession, Depends(get_session)]

_bearer_scheme = HTTPBearer(
    scheme_name="ApiKeyBearer",
    description="An API key minted by `ledger.admin.keys mint`, sent as a Bearer token.",
    auto_error=False,
)


class ApiKeyCache:
    """A small TTL cache from key hash to lookup result, so a hot path
    (every authenticated request) doesn't cost a database round trip on
    top of the DB work the route itself does -- that round trip would show
    up directly in the Locust p99 this phase is asked to report.

    Deliberately includes negative caching (`None` results): without it, an
    attacker probing random Bearer values turns every guess into a
    database query, i.e. the cache would make credential-stuffing cheaper
    to send *and* more expensive to receive.

    Lives on `app.state`, one instance per `create_app()` call -- never a
    module-level global, or one test's cached result could leak into the
    next test's fresh database (see `tests/conftest.py::app_client`,
    which builds a new `create_app()`, and therefore a new cache, per test).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_size: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._clock = clock
        self._entries: dict[str, tuple[AuthenticatedKey | None, float]] = {}

    def get(self, key_hash: str) -> tuple[bool, AuthenticatedKey | None]:
        """`(hit, value)` -- `hit=False` means "look it up", regardless of
        what `value` (always `None` on a miss) holds."""
        if self._ttl <= 0:
            return False, None
        entry = self._entries.get(key_hash)
        if entry is None:
            return False, None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key_hash]
            return False, None
        return True, value

    def put(self, key_hash: str, value: AuthenticatedKey | None) -> None:
        if self._ttl <= 0:
            return
        if len(self._entries) >= self._max_size and key_hash not in self._entries:
            self._evict_one()
        self._entries[key_hash] = (value, self._clock() + self._ttl)

    def _evict_one(self) -> None:
        oldest_hash = min(self._entries, key=lambda h: self._entries[h][1])
        del self._entries[oldest_hash]


def get_api_key_cache(request: Request) -> ApiKeyCache:
    cache: ApiKeyCache = request.app.state.api_key_cache
    return cache


async def require_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    cache: Annotated[ApiKeyCache, Depends(get_api_key_cache)],
    session: _SessionDep,
) -> AuthenticatedKey:
    """Depends on `get_session` (via the FastAPI dependency system, not a
    direct call) so `app.dependency_overrides[get_session]` -- how every
    test client points the app at its own engine/pool -- covers this
    lookup for free, the same as every other route."""
    if credentials is None:
        raise Unauthenticated("Missing or malformed Authorization header")

    key_hash = hash_api_key(credentials.credentials)
    hit, authenticated = cache.get(key_hash)
    if not hit:
        authenticated = await lookup_active_key(session, key_hash)
        cache.put(key_hash, authenticated)

    if authenticated is None:
        raise Unauthenticated("Unknown or inactive API key")

    request.state.api_key_id = authenticated.id
    return authenticated


ApiKeyDep = Annotated[AuthenticatedKey, Depends(require_api_key)]
