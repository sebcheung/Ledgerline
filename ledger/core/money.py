"""A minor-units money value type.

Money is an *edge* type: it exists to parse/format amounts at the API/CLI
boundary and to express invariant checks readably. The posting hot path
(`ledger.core.posting`) works in plain `int` minor-unit deltas keyed by
account id, because step 1 of posting has already proven every entry in a
transaction shares one currency -- constructing `Money` objects there would
add allocations without adding safety. See `docs/DECISIONS.md`.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import ClassVar

from ledger.core.errors import CurrencyMismatch, InvalidCurrency, InvalidMoney

#: Bigint domain, matching every `amount`/`balance` column in the schema.
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1

DEFAULT_MINOR_UNIT_EXPONENT = 2

#: Currencies whose minor-unit exponent differs from the common default of 2.
#: Deliberately not a full ISO 4217 registry -- multi-currency FX conversion
#: is an explicit SPEC.md non-goal, so this table exists only to keep
#: `from_decimal_string`/`to_decimal_string` correct for the currencies that
#: actually differ (a fixed 2-decimal assumption silently corrupts JPY by
#: 100x and KWD by 10x).
MINOR_UNIT_EXPONENT: "MappingProxyType[str, int]" = MappingProxyType(
    {
        # exponent 0 -- no minor unit in common use
        "BIF": 0,
        "CLP": 0,
        "DJF": 0,
        "GNF": 0,
        "ISK": 0,
        "JPY": 0,
        "KMF": 0,
        "KRW": 0,
        "PYG": 0,
        "RWF": 0,
        "UGX": 0,
        "UYI": 0,
        "VND": 0,
        "VUV": 0,
        "XAF": 0,
        "XOF": 0,
        "XPF": 0,
        # exponent 3
        "BHD": 3,
        "IQD": 3,
        "JOD": 3,
        "KWD": 3,
        "LYD": 3,
        "OMR": 3,
        "TND": 3,
        # exponent 4
        "CLF": 4,
        "UYW": 4,
    }
)

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
#: Plain integer or decimal, optionally signed. No scientific notation --
#: "1E3" is ambiguous to a human reader of a payments amount and must be
#: rejected explicitly rather than accepted by `Decimal`.
_DECIMAL_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def minor_unit_exponent(currency: str) -> int:
    return MINOR_UNIT_EXPONENT.get(currency, DEFAULT_MINOR_UNIT_EXPONENT)


def _validate_currency(currency: str) -> None:
    if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency):
        raise InvalidCurrency(
            f"currency must match ^[A-Z]{{3}}$, got {currency!r}", currency=currency
        )


@dataclass(frozen=True, slots=True)
class Money:
    """A signed amount in minor units of a single ISO 4217 currency.

    Negative amounts are legal -- this type also represents balances and
    deltas, not only entry amounts (entries additionally require
    `amount > 0`, enforced in `ledger.core.invariants`, not here).
    """

    amount: int
    currency: str

    _BIGINT_MIN: ClassVar[int] = _BIGINT_MIN
    _BIGINT_MAX: ClassVar[int] = _BIGINT_MAX

    def __post_init__(self) -> None:
        # bool is a subclass of int; explicitly reject it before the
        # isinstance/type check below could let it slip through as 0 or 1.
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise InvalidMoney(
                f"Money.amount must be int minor units; got {type(self.amount).__name__}",
                value=repr(self.amount),
            )
        if not (self._BIGINT_MIN <= self.amount <= self._BIGINT_MAX):
            raise InvalidMoney(
                f"Money.amount {self.amount} is outside the bigint domain", value=self.amount
            )
        _validate_currency(self.currency)

    def __add__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if other.currency != self.currency:
            raise CurrencyMismatch(
                "cannot add Money of different currencies",
                expected=self.currency,
                actual=other.currency,
            )
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: object) -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if other.currency != self.currency:
            raise CurrencyMismatch(
                "cannot subtract Money of different currencies",
                expected=self.currency,
                actual=other.currency,
            )
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    @staticmethod
    def total(values: "list[Money] | tuple[Money, ...]", currency: str) -> "Money":
        """Sum an iterable of `Money`, seeded at zero. Plain `sum()` fails
        because it starts from the int literal `0`, which `Money.__radd__`
        does not (and should not) special-case."""
        result = Money.zero(currency)
        for value in values:
            result = result + value
        return result

    @classmethod
    def from_decimal_string(cls, s: str, currency: str) -> "Money":
        if not isinstance(s, str):
            raise InvalidMoney(
                f"Money.from_decimal_string requires str input; got {type(s).__name__}",
                value=repr(s),
            )
        _validate_currency(currency)
        if not _DECIMAL_PATTERN.fullmatch(s):
            raise InvalidMoney(f"{s!r} is not a plain decimal string", value=s)
        try:
            value = Decimal(s)
        except InvalidOperation as exc:
            raise InvalidMoney(f"{s!r} could not be parsed as a decimal", value=s) from exc
        if not value.is_finite():
            raise InvalidMoney(f"{s!r} is not finite", value=s)

        exponent = minor_unit_exponent(currency)
        scaled = value.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise InvalidMoney(
                f"{s!r} has more precision than {currency} supports "
                f"({exponent} decimal place(s)); refusing to round",
                value=s,
                currency=currency,
            )
        return cls(int(scaled), currency)

    def to_decimal_string(self) -> str:
        exponent = minor_unit_exponent(self.currency)
        value = Decimal(self.amount).scaleb(-exponent)
        return f"{value:.{exponent}f}"
