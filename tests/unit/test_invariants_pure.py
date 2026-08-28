from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from ledger.core.errors import UnbalancedTransaction
from ledger.core.invariants import assert_transaction_balanced
from ledger.models.enums import EntryDirection


@dataclass(frozen=True, slots=True)
class _Leg:
    account_id: UUID
    direction: EntryDirection
    amount: int
    currency: str = "USD"


def _leg(direction: EntryDirection, amount: int, currency: str = "USD") -> _Leg:
    return _Leg(account_id=uuid4(), direction=direction, amount=amount, currency=currency)


def test_balanced_two_entry() -> None:
    assert_transaction_balanced([_leg(EntryDirection.DEBIT, 100), _leg(EntryDirection.CREDIT, 100)])


def test_balanced_four_entry_split() -> None:
    assert_transaction_balanced(
        [
            _leg(EntryDirection.DEBIT, 30),
            _leg(EntryDirection.DEBIT, 70),
            _leg(EntryDirection.CREDIT, 100),
        ]
    )


def test_off_by_one_rejected() -> None:
    with pytest.raises(UnbalancedTransaction) as exc_info:
        assert_transaction_balanced(
            [_leg(EntryDirection.DEBIT, 100), _leg(EntryDirection.CREDIT, 99)]
        )
    assert exc_info.value.extra["debit_total"] == 100
    assert exc_info.value.extra["credit_total"] == 99


def test_all_debits_rejected() -> None:
    with pytest.raises(UnbalancedTransaction):
        assert_transaction_balanced([_leg(EntryDirection.DEBIT, 100)])


def test_all_credits_rejected() -> None:
    with pytest.raises(UnbalancedTransaction):
        assert_transaction_balanced([_leg(EntryDirection.CREDIT, 100)])


def test_empty_is_balanced_trivially() -> None:
    # Shape validation (>= 2 entries) is posting.py's job, not this
    # function's -- an empty list sums to 0 == 0 and is "balanced".
    assert_transaction_balanced([])


def test_unequal_split_both_sides_balances() -> None:
    assert_transaction_balanced(
        [
            _leg(EntryDirection.DEBIT, 40),
            _leg(EntryDirection.DEBIT, 60),
            _leg(EntryDirection.CREDIT, 25),
            _leg(EntryDirection.CREDIT, 75),
        ]
    )


def test_large_amounts_do_not_overflow_python_ints() -> None:
    huge = 2**63 - 1
    assert_transaction_balanced(
        [_leg(EntryDirection.DEBIT, huge), _leg(EntryDirection.CREDIT, huge)]
    )
