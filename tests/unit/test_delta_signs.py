import pytest

from ledger.core.invariants import signed_delta
from ledger.models.enums import AccountType, EntryDirection

_EXPECTED_SIGNS: dict[tuple[AccountType, EntryDirection], int] = {
    (AccountType.ASSET, EntryDirection.DEBIT): +1,
    (AccountType.ASSET, EntryDirection.CREDIT): -1,
    (AccountType.EXPENSE, EntryDirection.DEBIT): +1,
    (AccountType.EXPENSE, EntryDirection.CREDIT): -1,
    (AccountType.LIABILITY, EntryDirection.DEBIT): -1,
    (AccountType.LIABILITY, EntryDirection.CREDIT): +1,
    (AccountType.EQUITY, EntryDirection.DEBIT): -1,
    (AccountType.EQUITY, EntryDirection.CREDIT): +1,
    (AccountType.REVENUE, EntryDirection.DEBIT): -1,
    (AccountType.REVENUE, EntryDirection.CREDIT): +1,
}


def test_every_account_type_and_direction_is_covered() -> None:
    assert set(_EXPECTED_SIGNS) == {(t, d) for t in AccountType for d in EntryDirection}


@pytest.mark.parametrize(("key", "sign"), _EXPECTED_SIGNS.items())
def test_balance_delta_sign(key: tuple[AccountType, EntryDirection], sign: int) -> None:
    account_type, direction = key
    assert signed_delta(account_type, direction, 100) == sign * 100


@pytest.mark.parametrize("account_type", list(AccountType))
def test_delta_magnitude_equals_amount(account_type: AccountType) -> None:
    assert abs(signed_delta(account_type, EntryDirection.DEBIT, 12345)) == 12345
    assert abs(signed_delta(account_type, EntryDirection.CREDIT, 12345)) == 12345
