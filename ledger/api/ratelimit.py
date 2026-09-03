"""In-process per-key token bucket rate limiting (SPEC.md §9 Phase 7: 100
req/s, burst 200).

Deliberately in-process, matching SPEC.md's wording exactly -- not Redis or
any other shared store. The consequence is that the effective limit is
per-*worker* process, not per-deployment; `fly.toml` (Phase 7's deploy
slice) therefore pins exactly one uvicorn worker per machine so "100/s
burst 200" stays true for the topology this ships to, rather than becoming
N times looser for N workers. See `docs/DECISIONS.md` Phase 7.

State lives on `app.state.rate_limiter`, one instance per `create_app()`
call, for the same reason `ledger.api.auth.ApiKeyCache` does: a
module-level global would let one test's drained bucket bleed into the
next test's fresh app.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from ledger.api.auth import ApiKeyDep
from ledger.config import get_settings
from ledger.core.errors import RateLimited


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """`acquire(key)` returns `None` if a request is allowed (a token was
    consumed), or the number of seconds until the next token would be
    available. Refill is computed lazily on read -- no background task, no
    timer -- so `acquire` is the only method that ever touches a bucket's
    state.
    """

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: float,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 4096,
    ) -> None:
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: dict[str, _TokenBucket] = {}

    def acquire(self, key: str) -> float | None:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_buckets:
                self._evict_oldest()
            bucket = _TokenBucket(tokens=self._burst, updated_at=now)
            self._buckets[key] = bucket
        else:
            elapsed = now - bucket.updated_at
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None

        missing = 1 - bucket.tokens
        return missing / self._rate

    def _evict_oldest(self) -> None:
        oldest_key = min(self._buckets, key=lambda k: self._buckets[k].updated_at)
        del self._buckets[oldest_key]


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


async def enforce_rate_limit(
    api_key: ApiKeyDep,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    # Read dynamically (matching dashboard/views.py's demo_enabled check),
    # not baked into RateLimiter at construction time -- unlike auth, a kill
    # switch here is defensible (it degrades availability, not security),
    # and the Locust load test (Phase 7) needs to disable it for a clean run.
    if not get_settings().rate_limit_enabled:
        return
    retry_after = limiter.acquire(str(api_key.id))
    if retry_after is not None:
        raise RateLimited("Rate limit exceeded", retry_after_seconds=retry_after)
