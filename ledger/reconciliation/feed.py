"""Settlement feed drift injection (SPEC.md §7, §12 Phase 4).

Framework-free, like the rest of `ledger.reconciliation`. Moved here from
`scripts/gen_feed.py` in Phase 6 so `dashboard/demo.py` -- which cannot
import `scripts/` (it ships in neither the wheel nor the Docker image, see
docs/DECISIONS.md Phase 6) -- can reuse the same drift logic
`scripts/gen_feed.py`'s CLI and `tests/faults/test_recon_drift.py` already
depend on, instead of a second implementation.
"""

import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime


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
