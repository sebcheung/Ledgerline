"""Idempotency key retention sweep CLI (SPEC.md §9 Phase 7):
`python -m ledger.admin.sweep [--days N]`.

A one-shot CLI, not a background loop -- run from a Fly scheduled machine
(`docs/DEPLOY.md`), not from `worker.webhook_worker`'s poll loop (a webhook
dispatcher sweeping idempotency keys would be a layering smell) and not
from an HTTP endpoint (a long DELETE behind a request timeout is the wrong
shape, and SPEC.md §9 names no such route). See `docs/DECISIONS.md` Phase 7.
"""

import argparse
import asyncio

from ledger.config import get_settings
from ledger.core.idempotency import sweep_idempotency_keys
from ledger.db.engine import engine
from ledger.db.session import async_session_factory


async def _sweep(days: int) -> None:
    async with async_session_factory() as session:
        deleted = await sweep_idempotency_keys(session, older_than_days=days)
        await session.commit()
    await engine.dispose()
    print(f"deleted {deleted} completed idempotency key(s) older than {days} day(s)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="override idempotency_retention_days (default: config setting)",
    )
    args = parser.parse_args(argv)
    days = args.days if args.days is not None else get_settings().idempotency_retention_days
    asyncio.run(_sweep(days))


if __name__ == "__main__":
    main()
