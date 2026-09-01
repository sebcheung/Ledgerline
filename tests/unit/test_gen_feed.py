"""Pure unit tests for `scripts.gen_feed.generate_feed` -- no DB required.
The fault suite (`tests/faults/test_recon_drift.py`) imports this same
function so drift injection is exercised by exactly one code path."""

import random
from datetime import date

from scripts.gen_feed import DriftConfig, FeedTransaction, generate_feed


def _txn(ref: str = "ref-1", amount: int = 1000, currency: str = "USD") -> FeedTransaction:
    return FeedTransaction(
        external_ref=ref, amount=amount, currency=currency, value_date=date(2026, 1, 1)
    )


def test_no_drift_produces_one_line_per_transaction() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(10)]
    lines = generate_feed(txns, DriftConfig(), random.Random(0))
    assert len(lines) == 10
    refs = {line.external_ref for line in lines}
    assert refs == {f"ref-{i}" for i in range(10)}


def test_same_seed_is_deterministic() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(50)]
    config = DriftConfig(drop_rate=0.3, duplicate_rate=0.2, perturb_rate=0.4, perturb_max_minor=50)
    lines_a = generate_feed(txns, config, random.Random(42))
    lines_b = generate_feed(txns, config, random.Random(42))
    assert [(line.external_ref, line.amount) for line in lines_a] == [
        (line.external_ref, line.amount) for line in lines_b
    ]


def test_different_seed_can_differ() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(50)]
    config = DriftConfig(drop_rate=0.5, perturb_rate=0.5, perturb_max_minor=100)
    lines_a = generate_feed(txns, config, random.Random(1))
    lines_b = generate_feed(txns, config, random.Random(2))
    assert lines_a != lines_b


def test_drop_rate_one_drops_every_line() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(20)]
    lines = generate_feed(txns, DriftConfig(drop_rate=1.0), random.Random(0))
    assert lines == []


def test_duplicate_rate_one_doubles_every_line() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(10)]
    lines = generate_feed(txns, DriftConfig(duplicate_rate=1.0), random.Random(0))
    assert len(lines) == 20


def test_perturbation_changes_amount_but_not_ref_or_currency() -> None:
    txns = [_txn(ref="ref-p", amount=1000)]
    lines = generate_feed(
        txns, DriftConfig(perturb_rate=1.0, perturb_max_minor=10), random.Random(0)
    )
    line = lines[0]
    assert line.external_ref == "ref-p"
    assert line.currency == "USD"
    assert line.amount != 1000
    assert abs(line.amount - 1000) <= 10


def test_extra_lines_have_no_matching_transaction() -> None:
    lines = generate_feed([], DriftConfig(extra_lines=5), random.Random(0))
    assert len(lines) == 5
    refs = {line.external_ref for line in lines}
    assert len(refs) == 5  # each synthetic ref is unique
    assert all(
        line.external_ref is not None and line.external_ref.startswith("synthetic-")
        for line in lines
    )


def test_zero_rates_are_a_no_op() -> None:
    txns = [_txn(ref=f"ref-{i}") for i in range(5)]
    lines = generate_feed(txns, DriftConfig(), random.Random(0))
    assert [(line.external_ref, line.amount, line.currency, line.value_date) for line in lines] == [
        (t.external_ref, t.amount, t.currency, t.value_date) for t in txns
    ]
