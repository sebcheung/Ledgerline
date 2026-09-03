"""Unit tests for `ledger.api.auth.ApiKeyCache`, all against an injected
fake clock -- no DB, no real time.monotonic() sleeps."""

import uuid

from ledger.api.auth import ApiKeyCache
from ledger.core.apikeys import AuthenticatedKey


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _key(name: str = "k") -> AuthenticatedKey:
    return AuthenticatedKey(id=uuid.uuid4(), name=name)


def test_miss_on_empty_cache() -> None:
    cache = ApiKeyCache(ttl_seconds=30.0, clock=FakeClock())
    hit, value = cache.get("h1")
    assert not hit
    assert value is None


def test_put_then_get_hits_before_ttl_expires() -> None:
    clock = FakeClock()
    cache = ApiKeyCache(ttl_seconds=30.0, clock=clock)
    key = _key()
    cache.put("h1", key)

    clock.now = 29.9
    hit, value = cache.get("h1")
    assert hit
    assert value == key


def test_entry_expires_after_ttl() -> None:
    clock = FakeClock()
    cache = ApiKeyCache(ttl_seconds=30.0, clock=clock)
    cache.put("h1", _key())

    clock.now = 30.0
    hit, value = cache.get("h1")
    assert not hit
    assert value is None


def test_negative_caching_stores_none() -> None:
    clock = FakeClock()
    cache = ApiKeyCache(ttl_seconds=30.0, clock=clock)
    cache.put("unknown-hash", None)

    hit, value = cache.get("unknown-hash")
    assert hit
    assert value is None


def test_ttl_zero_disables_the_cache() -> None:
    cache = ApiKeyCache(ttl_seconds=0.0, clock=FakeClock())
    cache.put("h1", _key())

    hit, _ = cache.get("h1")
    assert not hit


def test_capacity_eviction_drops_the_oldest_entry() -> None:
    clock = FakeClock()
    cache = ApiKeyCache(ttl_seconds=100.0, max_size=2, clock=clock)

    cache.put("h1", _key("first"))
    clock.now = 1.0
    cache.put("h2", _key("second"))
    clock.now = 2.0
    cache.put("h3", _key("third"))  # should evict h1, the oldest expiry

    hit1, _ = cache.get("h1")
    hit2, _ = cache.get("h2")
    hit3, _ = cache.get("h3")
    assert not hit1
    assert hit2
    assert hit3


def test_updating_an_existing_key_does_not_count_against_capacity() -> None:
    clock = FakeClock()
    cache = ApiKeyCache(ttl_seconds=100.0, max_size=2, clock=clock)
    cache.put("h1", _key("first"))
    cache.put("h2", _key("second"))
    cache.put("h1", _key("first-updated"))  # refresh, not a new entry

    hit1, value1 = cache.get("h1")
    hit2, _ = cache.get("h2")
    assert hit1
    assert value1 is not None
    assert value1.name == "first-updated"
    assert hit2
