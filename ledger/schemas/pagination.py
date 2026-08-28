"""Opaque, versioned, unsigned keyset-pagination cursor.

`created_at` ties across entries of the same transaction are *guaranteed*,
not merely possible: Postgres `now()` is transaction-start time, constant
for every statement in one DB transaction, so every entry inserted by one
`post_transaction` call shares an identical `created_at`. A `created_at`
-only keyset would therefore skip or duplicate rows at every transaction
boundary; the tiebreak on `id` is load-bearing, not defensive.

OFFSET pagination is rejected outright: it duplicates or skips rows under
concurrent inserts, which a ledger accumulates continuously.

The cursor is not signed or encrypted -- it encodes only `(created_at, id)`
values the client already received in the page it came from, so there is
nothing confidential or forgeable-with-consequence to protect. It carries a
version tag so the encoding can change later without silently
mis-paginating an old client's stored cursor.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ledger.core.errors import InvalidCursor

CURSOR_VERSION = "v1"

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: UUID


def encode_cursor(created_at: datetime, id_: UUID) -> str:
    raw = json.dumps(
        {"v": CURSOR_VERSION, "t": created_at.isoformat(), "i": str(id_)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> Cursor:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if data.get("v") != CURSOR_VERSION:
            raise InvalidCursor(f"unsupported cursor version {data.get('v')!r}")
        return Cursor(created_at=datetime.fromisoformat(data["t"]), id=UUID(data["i"]))
    except InvalidCursor:
        raise
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        raise InvalidCursor("cursor is malformed") from exc


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None
    has_more: bool
