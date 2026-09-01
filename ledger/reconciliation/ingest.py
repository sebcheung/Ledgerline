"""Settlement feed ingest (SPEC.md §7 `ingest.py`, `POST /v1/settlements/ingest`).

Framework-free: never imports `fastapi`, same rule as `ledger.core` -- the
route in `ledger/api/routes/settlements.py` is a thin wrapper around
`ingest_batch`.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.settlements import SettlementLine
from ledger.schemas.settlements import SettlementLineIn


@dataclass(frozen=True, slots=True)
class IngestResult:
    batch_id: uuid.UUID
    ingested: int
    deduplicated: int


def _dedup_key(line: SettlementLineIn) -> tuple[str, int, str, str] | None:
    """SPEC.md §7 dedups within a batch on `(external_ref, amount,
    value_date)`. Currency is added here -- a documented deviation
    (docs/DECISIONS.md Phase 4): two same-day, same-amount, same-ref lines
    are legitimately distinct if they're in different currencies, which
    §7's literal tuple would collide. Returns None for a NULL
    `external_ref`, so the caller never dedups those against each other --
    an unrefed line can't be distinguished from a genuinely distinct one
    within a batch."""
    if line.external_ref is None:
        return None
    return (line.external_ref, line.amount, line.currency, line.value_date.isoformat())


async def ingest_batch(session: AsyncSession, lines: list[SettlementLineIn]) -> IngestResult:
    """Assign one `batch_id` to `lines`, drop in-batch duplicates, insert
    the survivors, and store each line's original payload verbatim in
    `raw`. Never commits -- the caller (the route handler) owns the
    transaction boundary, same convention as `ledger.core.posting`."""
    batch_id = uuid.uuid4()

    seen: set[tuple[str, int, str, str]] = set()
    survivors: list[SettlementLineIn] = []
    deduplicated = 0
    for line in lines:
        key = _dedup_key(line)
        if key is not None and key in seen:
            deduplicated += 1
            continue
        if key is not None:
            seen.add(key)
        survivors.append(line)

    if survivors:
        await session.execute(
            insert(SettlementLine).values(
                [
                    {
                        "external_ref": line.external_ref,
                        "amount": line.amount,
                        "currency": line.currency,
                        "value_date": line.value_date,
                        "raw": line.raw,
                        "batch_id": batch_id,
                    }
                    for line in survivors
                ]
            )
        )

    return IngestResult(batch_id=batch_id, ingested=len(survivors), deduplicated=deduplicated)
