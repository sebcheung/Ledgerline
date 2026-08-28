from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ledger.core.errors import InvalidCursor
from ledger.schemas.pagination import decode_cursor, encode_cursor


def test_round_trip() -> None:
    now = datetime(2026, 8, 28, 12, 30, 0, tzinfo=UTC)
    id_ = uuid4()
    token = encode_cursor(now, id_)
    cursor = decode_cursor(token)
    assert cursor.created_at == now
    assert cursor.id == id_


def test_token_is_url_safe_and_unpadded() -> None:
    token = encode_cursor(datetime.now(UTC), uuid4())
    assert "=" not in token
    assert "+" not in token
    assert "/" not in token


@pytest.mark.parametrize(
    "bad",
    [
        "not-valid-base64!!!",
        "",
        "dGhpcyBpcyBub3QganNvbg",  # valid base64, not JSON
        "e30",  # valid base64 JSON `{}` -- missing keys
    ],
)
def test_decode_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_decode_rejects_wrong_version() -> None:
    import base64
    import json

    raw = json.dumps({"v": "v99", "t": "2026-01-01T00:00:00+00:00", "i": str(uuid4())})
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(token)


def test_cursor_is_opaque_not_a_raw_integer() -> None:
    # Guards against an offset-style cursor, which is unstable under
    # concurrent inserts.
    token = encode_cursor(datetime.now(UTC), uuid4())
    assert not token.isdigit()
