"""Generate a settlement feed with configurable, seeded drift (SPEC.md §7,
§12 Phase 4).

The drift logic (`generate_feed`) is a pure function over a list of
`FeedTransaction` and a `random.Random` -- `tests/faults/test_recon_drift.py`
imports it directly so the fault suite exercises the same code path this
CLI does, instead of reimplementing drift injection.
"""

import argparse
import asyncio
import json
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import BigInteger, case, cast, func, select

from ledger.models.entries import Entry
from ledger.models.enums import EntryDirection, TransactionSource, TransactionStatus
from ledger.models.transactions import Transaction


@dataclass(frozen=True, slots=True)
class FeedTransaction:
    """The externally-visible shape of a ledger transaction, as a
    settlement feed would see it -- everything `generate_feed` needs and
    nothing it shouldn't have (no transaction id: a settlement line never
    carries one)."""

    external_ref: str | None
    amount: int
    currency: str
    value_date: date


@dataclass(frozen=True, slots=True)
class DriftConfig:
    drop_rate: float = 0.0
    duplicate_rate: float = 0.0
    perturb_rate: float = 0.0
    perturb_max_minor: int = 0
    #: Lines with no corresponding transaction at all -- becomes
    #: `unexpected_settlement`.
    extra_lines: int = 0


@dataclass(frozen=True, slots=True)
class GeneratedLine:
    external_ref: str | None
    amount: int
    currency: str
    value_date: date
    raw: dict[str, object]


def generate_feed(
    transactions: Sequence[FeedTransaction], config: DriftConfig, rng: random.Random
) -> list[GeneratedLine]:
    lines: list[GeneratedLine] = []
    for txn in transactions:
        if rng.random() < config.drop_rate:
            continue  # SPEC.md §10 "feed with dropped lines"

        amount = txn.amount
        if config.perturb_max_minor > 0 and rng.random() < config.perturb_rate:
            # Excludes 0 -- a zero perturbation is not drift.
            delta = rng.choice(
                [d for d in range(-config.perturb_max_minor, config.perturb_max_minor + 1) if d]
            )
            amount += delta

        line = GeneratedLine(
            external_ref=txn.external_ref,
            amount=amount,
            currency=txn.currency,
            value_date=txn.value_date,
            raw={
                "external_ref": txn.external_ref,
                "amount": amount,
                "currency": txn.currency,
                "value_date": txn.value_date.isoformat(),
            },
        )
        lines.append(line)

        if rng.random() < config.duplicate_rate:
            # An exact duplicate of the *undrifted* line -- SPEC.md §10
            # "feed with duplicated lines".
            lines.append(
                GeneratedLine(
                    external_ref=txn.external_ref,
                    amount=txn.amount,
                    currency=txn.currency,
                    value_date=txn.value_date,
                    raw={
                        "external_ref": txn.external_ref,
                        "amount": txn.amount,
                        "currency": txn.currency,
                        "value_date": txn.value_date.isoformat(),
                    },
                )
            )

    for _ in range(config.extra_lines):
        ref = f"synthetic-{uuid.uuid4()}"
        amount = rng.randint(100, 1_000_00)
        lines.append(
            GeneratedLine(
                external_ref=ref,
                amount=amount,
                currency="USD",
                value_date=datetime.now(UTC).date(),
                raw={"external_ref": ref, "amount": amount, "currency": "USD", "synthetic": True},
            )
        )

    return lines


async def _load_recent_transactions(database_url: str, window_days: int) -> list[FeedTransaction]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            stmt = (
                select(
                    Transaction.external_ref,
                    Transaction.created_at,
                    func.min(Entry.currency).label("currency"),
                    cast(
                        func.sum(
                            case((Entry.direction == EntryDirection.DEBIT, Entry.amount), else_=0)
                        ),
                        BigInteger,
                    ).label("amount"),
                )
                .join(Entry, Entry.transaction_id == Transaction.id)
                .where(
                    Transaction.status == TransactionStatus.POSTED,
                    Transaction.source == TransactionSource.API,
                    Transaction.reversal_of.is_(None),
                    Transaction.created_at >= datetime.now(UTC) - timedelta(days=window_days),
                )
                .group_by(Transaction.id, Transaction.external_ref, Transaction.created_at)
            )
            rows = (await session.execute(stmt)).all()
            return [
                FeedTransaction(
                    external_ref=r.external_ref,
                    amount=r.amount,
                    currency=r.currency,
                    value_date=r.created_at.date(),
                )
                for r in rows
            ]
    finally:
        await engine.dispose()


def _write_feed(lines: list[GeneratedLine], out_path: str) -> None:
    document = {
        "lines": [
            {
                "external_ref": line.external_ref,
                "amount": line.amount,
                "currency": line.currency,
                "value_date": line.value_date.isoformat(),
                "raw": line.raw,
            }
            for line in lines
        ]
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2)


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--drop-rate", type=float, default=0.0)
    parser.add_argument("--duplicate-rate", type=float, default=0.0)
    parser.add_argument("--perturb-rate", type=float, default=0.0)
    parser.add_argument("--perturb-max-minor", type=int, default=0)
    parser.add_argument("--extra-lines", type=int, default=0)
    parser.add_argument("--out", default="feed.json")
    args = parser.parse_args()

    from ledger.config import get_settings

    database_url = get_settings().database_url
    transactions = asyncio.run(_load_recent_transactions(database_url, args.window_days))

    config = DriftConfig(
        drop_rate=args.drop_rate,
        duplicate_rate=args.duplicate_rate,
        perturb_rate=args.perturb_rate,
        perturb_max_minor=args.perturb_max_minor,
        extra_lines=args.extra_lines,
    )
    lines = generate_feed(transactions, config, random.Random(args.seed))
    _write_feed(lines, args.out)
    print(
        f"wrote {len(lines)} settlement lines from {len(transactions)} transactions to {args.out}"
    )


if __name__ == "__main__":
    _main()
