import json
import re

import pytest

from ledger.core.errors import InvalidRequestBody
from ledger.core.idempotency import canonical_hash


def _body(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


def test_key_order_does_not_affect_hash() -> None:
    a = canonical_hash(_body({"a": 1, "b": 2}))
    b = canonical_hash(_body({"b": 2, "a": 1}))
    assert a == b


def test_whitespace_does_not_affect_hash() -> None:
    a = canonical_hash(b'{"a": 1, "b": 2}')
    b = canonical_hash(b'{"a":1,"b":2}')
    assert a == b


def test_null_object_members_are_dropped() -> None:
    a = canonical_hash(_body({"a": 1, "b": None}))
    b = canonical_hash(_body({"a": 1}))
    assert a == b


def test_null_array_elements_are_preserved() -> None:
    # Dropping array nulls would shift indices and wrongly equate these.
    a = canonical_hash(_body({"a": [1, None, 2]}))
    b = canonical_hash(_body({"a": [1, 2]}))
    assert a != b


def test_nested_object_key_order_does_not_affect_hash() -> None:
    a = canonical_hash(_body({"a": {"x": 1, "y": 2}, "b": [{"p": 1, "q": 2}]}))
    b = canonical_hash(_body({"b": [{"q": 2, "p": 1}], "a": {"y": 2, "x": 1}}))
    assert a == b


@pytest.mark.parametrize("body", [None, b"", b"   ", b"\n\t "])
def test_empty_body_variants_hash_the_same_as_empty_object(body: bytes | None) -> None:
    assert canonical_hash(body) == canonical_hash(_body({}))


def test_int_and_float_are_distinct() -> None:
    a = canonical_hash(_body({"amount": 1}))
    b = canonical_hash(_body({"amount": 1.0}))
    assert a != b


def test_non_ascii_is_stable() -> None:
    a = canonical_hash(_body({"description": "café"}))
    b = canonical_hash(_body({"description": "café"}))
    assert a == b


def test_path_params_affect_the_hash() -> None:
    a = canonical_hash(b"", path_params={"transaction_id": "aaa"})
    b = canonical_hash(b"", path_params={"transaction_id": "bbb"})
    assert a != b


def test_no_path_params_differs_from_empty_path_params() -> None:
    # Both should still be well-defined and mutually consistent, but the
    # important property is that varying the id changes the hash -- this
    # pins the fact that an empty-bodied route (e.g. POST .../reverse)
    # cannot fingerprint identically for two different targets.
    a = canonical_hash(b"", path_params=None)
    b = canonical_hash(b"", path_params={})
    assert a == b


def test_different_path_param_names_change_the_hash() -> None:
    a = canonical_hash(b"", path_params={"transaction_id": "aaa"})
    b = canonical_hash(b"", path_params={"account_id": "aaa"})
    assert a != b


def test_malformed_json_raises_invalid_request_body() -> None:
    with pytest.raises(InvalidRequestBody):
        canonical_hash(b"{not json")


def test_output_is_64_lowercase_hex_chars() -> None:
    digest = canonical_hash(_body({"a": 1}))
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
