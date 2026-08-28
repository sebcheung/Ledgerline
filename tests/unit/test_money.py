import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger.core.errors import CurrencyMismatch, InvalidCurrency, InvalidMoney
from ledger.core.money import Money


def test_add_same_currency() -> None:
    assert Money(100, "USD") + Money(50, "USD") == Money(150, "USD")


def test_sub_same_currency() -> None:
    assert Money(100, "USD") - Money(50, "USD") == Money(50, "USD")


def test_neg() -> None:
    assert -Money(100, "USD") == Money(-100, "USD")


def test_abs() -> None:
    assert abs(Money(-100, "USD")) == Money(100, "USD")


@pytest.mark.parametrize(
    ("a", "b"),
    [("USD", "EUR"), ("USD", "usd"), ("USD", "US")],
)
def test_add_cross_currency_raises(a: str, b: str) -> None:
    with pytest.raises((CurrencyMismatch, InvalidCurrency)):
        Money(100, a) + Money(100, b)


def test_sub_cross_currency_raises() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") - Money(100, "EUR")


def test_neg_preserves_currency() -> None:
    assert (-Money(5, "EUR")).currency == "EUR"


def test_zero_is_allowed_as_a_value() -> None:
    # Money(0, ...) is a legal balance/delta. Rejecting a zero-amount entry
    # is an entry-shape rule, enforced in ledger.core.invariants, not here.
    assert Money(0, "USD").amount == 0


def test_negative_amount_allowed() -> None:
    assert Money(-100, "USD").amount == -100


def test_frozen() -> None:
    m = Money(1, "USD")
    with pytest.raises(AttributeError):
        m.amount = 2  # type: ignore[misc]


def test_hashable() -> None:
    assert len({Money(1, "USD"), Money(1, "USD"), Money(2, "USD")}) == 2


def test_equality_requires_currency() -> None:
    assert Money(100, "USD") != Money(100, "EUR")


def test_add_returns_not_implemented_for_non_money() -> None:
    with pytest.raises(TypeError):
        Money(1, "USD") + 1


def test_no_float_or_bool_constructor() -> None:
    with pytest.raises(InvalidMoney):
        Money(1.0, "USD")  # type: ignore[arg-type]
    with pytest.raises(InvalidMoney):
        Money(True, "USD")


def test_bigint_domain() -> None:
    Money(2**63 - 1, "USD")
    Money(-(2**63), "USD")
    with pytest.raises(InvalidMoney):
        Money(2**63, "USD")
    with pytest.raises(InvalidMoney):
        Money(-(2**63) - 1, "USD")


@pytest.mark.parametrize(
    ("s", "currency", "expected"),
    [
        ("0.00", "USD", 0),
        ("1.00", "USD", 100),
        ("-1.23", "USD", -123),
        ("0.01", "USD", 1),
        ("1", "USD", 100),
        ("1.5", "USD", 150),
        ("100", "JPY", 100),
        ("1.234", "KWD", 1234),
    ],
)
def test_from_decimal_string_round_trip(s: str, currency: str, expected: int) -> None:
    assert Money.from_decimal_string(s, currency) == Money(expected, currency)


@pytest.mark.parametrize(
    "s",
    ["1.234", "abc", "", "1,00", "1e2", "1E3", "nan", "inf", " 1.00 ", "1."],
)
def test_from_decimal_string_rejects(s: str) -> None:
    with pytest.raises(InvalidMoney):
        Money.from_decimal_string(s, "USD")


def test_from_decimal_string_rejects_float_input() -> None:
    with pytest.raises(InvalidMoney):
        Money.from_decimal_string(1.00, "USD")  # type: ignore[arg-type]


def test_to_decimal_string_exponent_0() -> None:
    assert Money(100, "JPY").to_decimal_string() == "100"


def test_to_decimal_string_negative() -> None:
    assert Money(-5, "USD").to_decimal_string() == "-0.05"


def test_to_decimal_string_exponent_3() -> None:
    assert Money(1234, "KWD").to_decimal_string() == "1.234"


def test_total_seeds_at_zero() -> None:
    assert Money.total([], "USD") == Money.zero("USD")
    assert Money.total([Money(1, "USD"), Money(2, "USD")], "USD") == Money(3, "USD")


def test_invalid_currency_rejected() -> None:
    for bad in ("US", "usdollar", "", "123", "usd"):
        with pytest.raises(InvalidCurrency):
            Money(1, bad)


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_round_trip_property(amount: int) -> None:
    m = Money(amount, "USD")
    assert Money.from_decimal_string(m.to_decimal_string(), "USD") == m
