import hmac
import re
from hashlib import sha256

import pytest

from ledger.webhooks.signing import (
    SIGNATURE_PREFIX,
    sign,
    signature_header,
    signing_payload,
    verify,
)

SECRET = "test-secret"
TIMESTAMP = 1700000000
BODY = b'{"event_id":"abc","event_type":"transaction.posted"}'


def test_signing_payload_is_timestamp_dot_raw_body_bytes() -> None:
    # Pinned against a manually-computed expectation, not against
    # signing_payload's own implementation -- this is the exact trap the
    # module docstring warns about: an f-string over `bytes` interpolates
    # its repr, not its content.
    assert signing_payload(TIMESTAMP, BODY) == b"1700000000." + BODY


def test_signing_payload_is_not_the_repr_of_the_body() -> None:
    # If signing_payload ever regressed to f"{timestamp}.{raw_body}", this
    # would be the (wrong) result -- pin that it is NOT this.
    wrong = f"{TIMESTAMP}.{BODY!r}".encode()
    assert signing_payload(TIMESTAMP, BODY) != wrong


def test_sign_matches_a_known_answer_vector() -> None:
    # A checked-in literal, computed independently of sign()'s own code, so
    # a refactor of sign()/signing_payload() cannot silently change the
    # wire format without a test noticing.
    assert (
        sign(SECRET, TIMESTAMP, BODY)
        == "dc606e5e3e8784f9427a2c17e198f922be9f644250505e62928624867bd28542"
    )
    assert (
        sign(SECRET, TIMESTAMP, BODY)
        == hmac.new(SECRET.encode(), b"1700000000." + BODY, sha256).hexdigest()
    )


def test_sign_is_64_lowercase_hex_chars() -> None:
    digest = sign(SECRET, TIMESTAMP, BODY)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_signature_header_has_sha256_prefix() -> None:
    header = signature_header(SECRET, TIMESTAMP, BODY)
    assert header.startswith(SIGNATURE_PREFIX)
    assert header == f"sha256={sign(SECRET, TIMESTAMP, BODY)}"


def test_verify_accepts_a_correct_signature() -> None:
    header = signature_header(SECRET, TIMESTAMP, BODY)
    assert verify(SECRET, TIMESTAMP, BODY, header) is True


def test_verify_rejects_wrong_secret() -> None:
    header = signature_header(SECRET, TIMESTAMP, BODY)
    assert verify("wrong-secret", TIMESTAMP, BODY, header) is False


def test_verify_rejects_tampered_body() -> None:
    header = signature_header(SECRET, TIMESTAMP, BODY)
    assert verify(SECRET, TIMESTAMP, BODY + b"x", header) is False


def test_verify_rejects_tampered_timestamp() -> None:
    header = signature_header(SECRET, TIMESTAMP, BODY)
    assert verify(SECRET, TIMESTAMP + 1, BODY, header) is False


def test_verify_rejects_missing_prefix() -> None:
    bare_hex = sign(SECRET, TIMESTAMP, BODY)
    assert verify(SECRET, TIMESTAMP, BODY, bare_hex) is False


def test_verify_rejects_unknown_prefix() -> None:
    bare_hex = sign(SECRET, TIMESTAMP, BODY)
    assert verify(SECRET, TIMESTAMP, BODY, f"sha1={bare_hex}") is False


def test_verify_rejects_wrong_length_hex() -> None:
    assert verify(SECRET, TIMESTAMP, BODY, "sha256=deadbeef") is False


@pytest.mark.parametrize(
    "body",
    [
        b'{"description": "caf\xc3\xa9 \\"quoted\\""}',
        'café "quoted" 日本語'.encode(),
    ],
)
def test_sign_and_verify_round_trip_non_ascii_and_quoted_bodies(body: bytes) -> None:
    header = signature_header(SECRET, TIMESTAMP, body)
    assert verify(SECRET, TIMESTAMP, body, header) is True
