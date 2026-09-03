"""Unit tests for the pure (no-DB) half of `ledger.core.apikeys`: hashing
and generation. `create_api_key` / `lookup_active_key` / `revoke_api_key`
touch the database and are covered by
`tests/integration/test_auth.py` and `tests/integration/test_admin_keys_cli.py`.
"""

from ledger.core.apikeys import generate_api_key, hash_api_key


def test_generate_api_key_has_a_recognizable_prefix() -> None:
    assert generate_api_key().startswith("lk_")


def test_generate_api_key_is_unique_per_call() -> None:
    keys = {generate_api_key() for _ in range(100)}
    assert len(keys) == 100


def test_generate_api_key_has_high_entropy_length() -> None:
    # "lk_" + secrets.token_urlsafe(32) -- 43 base64url characters for 32
    # random bytes, plus the 3-character prefix.
    assert len(generate_api_key()) == 3 + 43


def test_hash_api_key_is_deterministic() -> None:
    raw = generate_api_key()
    assert hash_api_key(raw) == hash_api_key(raw)


def test_hash_api_key_differs_for_different_inputs() -> None:
    assert hash_api_key("a") != hash_api_key("b")


def test_hash_api_key_is_sha256_hex() -> None:
    digest = hash_api_key("known-input")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_api_key_never_equals_the_raw_key() -> None:
    raw = generate_api_key()
    assert hash_api_key(raw) != raw
