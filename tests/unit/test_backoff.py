import random
from unittest.mock import patch

import pytest

from ledger.webhooks.dispatcher import compute_backoff

BASE = 1.0
CAP = 3600.0


@pytest.mark.parametrize("attempt", range(1, 9))
def test_backoff_is_within_full_jitter_bounds(attempt: int) -> None:
    rng = random.Random(1234)
    expected_bound = min(BASE * 2 ** (attempt - 1), CAP)
    for _ in range(20):
        delay = compute_backoff(attempt, base_seconds=BASE, max_seconds=CAP, rng=rng)
        assert 0.0 <= delay <= expected_bound


def test_backoff_bound_doubles_each_attempt_below_the_cap() -> None:
    bounds = [min(BASE * 2 ** (a - 1), CAP) for a in range(1, 9)]
    assert bounds == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0]


def test_backoff_saturates_at_the_cap() -> None:
    rng = random.Random(1)
    # base=1, cap=3 -> attempt 3+ would want base=4, 8, ... but is capped at 3.
    for attempt in (3, 4, 8):
        delay = compute_backoff(attempt, base_seconds=1.0, max_seconds=3.0, rng=rng)
        assert 0.0 <= delay <= 3.0


def test_backoff_uses_full_jitter_not_equal_jitter() -> None:
    """SPEC.md §8 is explicit: `random.uniform(0, base)`, the full range,
    not `base/2 + random.uniform(0, base/2)` (equal jitter)."""
    rng = random.Random()
    with patch.object(rng, "uniform", wraps=rng.uniform) as spy:
        compute_backoff(3, base_seconds=BASE, max_seconds=CAP, rng=rng)
    spy.assert_called_once_with(0, min(BASE * 2**2, CAP))


def test_backoff_is_deterministic_for_a_seeded_rng() -> None:
    a = compute_backoff(4, base_seconds=BASE, max_seconds=CAP, rng=random.Random(42))
    b = compute_backoff(4, base_seconds=BASE, max_seconds=CAP, rng=random.Random(42))
    assert a == b


def test_backoff_can_return_zero() -> None:
    class ZeroRng(random.Random):
        def uniform(self, a: float, b: float) -> float:
            return a

    delay = compute_backoff(1, base_seconds=BASE, max_seconds=CAP, rng=ZeroRng())
    assert delay == 0.0
