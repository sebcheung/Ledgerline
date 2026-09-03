"""Unit tests for `ledger.api.ratelimit.RateLimiter`, all against an
injected fake clock -- no DB, no real sleeps."""

from ledger.api.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_burst_allows_up_to_capacity_then_denies() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=1.0, burst=3.0, clock=clock)

    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is None
    retry_after = limiter.acquire("k")
    assert retry_after is not None
    assert retry_after > 0


def test_retry_after_matches_exact_refill_arithmetic() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=2.0, burst=1.0, clock=clock)

    assert limiter.acquire("k") is None  # consumes the only token
    retry_after = limiter.acquire("k")
    # 0 tokens remaining, rate=2/s -> exactly 0.5s until one token refills.
    assert retry_after == 0.5


def test_tokens_refill_linearly_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=1.0, burst=1.0, clock=clock)

    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is not None  # exhausted

    clock.now = 1.0  # exactly one refill interval later
    assert limiter.acquire("k") is None


def test_refill_is_clamped_to_capacity() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=10.0, burst=2.0, clock=clock)

    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is None

    clock.now = 1000.0  # a huge elapsed gap must not overflow past burst
    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is not None  # still only 2 tokens/window


def test_buckets_are_independent_per_key() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=1.0, burst=1.0, clock=clock)

    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is not None  # a's bucket is empty
    assert limiter.acquire("b") is None  # b is untouched


def test_max_buckets_evicts_the_oldest_bucket() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=1.0, burst=1.0, clock=clock, max_buckets=2)

    limiter.acquire("a")
    clock.now = 1.0
    limiter.acquire("b")
    clock.now = 2.0
    limiter.acquire("c")  # should evict "a", the least-recently-updated

    # "a" got a fresh bucket (full burst) since its old one was evicted.
    assert limiter.acquire("a") is None
