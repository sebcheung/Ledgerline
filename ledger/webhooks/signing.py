"""HMAC-SHA256 request signing for outbound webhook deliveries (SPEC.md §8).

The signed string is `f"{timestamp}." + raw_body`, where `raw_body` is the
*exact bytes* transmitted on the wire. This must be built with byte
concatenation, not a Python f-string over the whole thing: an f-string
interpolates `bytes` via its `repr()` (`b'{"id": ...}'`, backslash escapes
and all), which produces a signature that is internally consistent but
matches no receiver implemented against the documented scheme. See
`signing_payload` below.

Callers must sign the exact bytes they send -- `ledger.webhooks.dispatcher`
serializes the envelope once via `serialize_envelope` and passes those same
bytes to both `sign()` and the HTTP client's request body, never re-encoding
in between (e.g. never `httpx`'s `json=` kwarg, which would re-serialize
with different separators and silently break the signature).
"""

import hmac
import json
import uuid
from hashlib import sha256
from typing import Any

SIGNATURE_ALGORITHM = "sha256"
SIGNATURE_PREFIX = f"{SIGNATURE_ALGORITHM}="


def build_envelope(event_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The wire shape delivered to a webhook endpoint. `payload` is already
    JSON-native (see `ledger.webhooks.outbox.transaction_event_payload`)."""
    return {"event_id": str(event_id), "event_type": event_type, "payload": payload}


def serialize_envelope(envelope: dict[str, Any]) -> bytes:
    """Byte-stable JSON serialization: sorted keys and compact separators,
    so the same envelope always produces the same bytes regardless of the
    dict's insertion order. This is what makes the signature reproducible
    -- sign these exact bytes, and send these exact bytes, never both
    independently."""
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signing_payload(timestamp: int, raw_body: bytes) -> bytes:
    """The exact byte string the HMAC is computed over."""
    return f"{timestamp}.".encode("ascii") + raw_body


def sign(secret: str, timestamp: int, raw_body: bytes) -> str:
    """Bare lowercase hex digest, no algorithm prefix."""
    return hmac.new(
        secret.encode("utf-8"), signing_payload(timestamp, raw_body), sha256
    ).hexdigest()


def signature_header(secret: str, timestamp: int, raw_body: bytes) -> str:
    """The full `X-Ledgerline-Signature` header value, e.g. `sha256=<hex>`."""
    return f"{SIGNATURE_PREFIX}{sign(secret, timestamp, raw_body)}"


def verify(secret: str, timestamp: int, raw_body: bytes, header_value: str) -> bool:
    """Constant-time verification for a receiver. A missing/unrecognized
    algorithm prefix is treated as an invalid signature, not an error --
    receivers should never distinguish "malformed header" from "wrong
    signature" in their response, since that distinction is itself
    information a forger could use.

    No timestamp-tolerance (replay window) check here -- that policy is the
    receiver's to set; this function only answers "does this signature match
    this exact (timestamp, body)".
    """
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False
    candidate = header_value[len(SIGNATURE_PREFIX) :]
    expected = sign(secret, timestamp, raw_body)
    return hmac.compare_digest(candidate, expected)
