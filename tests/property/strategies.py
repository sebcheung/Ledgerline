"""Hypothesis strategies for ledger scenarios.

Design rules, in priority order (see the Phase 2 plan for the full
rationale):

1. Generate indices, never UUIDs -- accounts are `0..n-1`; the runner maps
   index -> real UUID. UUIDs are opaque to the shrinker.
2. No `.filter()` on the hot path -- pick distinct accounts by construction.
3. Reversal targets are indices into the ops list, resolved modulo at
   execution time, so the shrinker can delete earlier ops without
   invalidating later ones.
4. Small numeric ranges that shrink toward 1.
5. Every generated value is a frozen, reproducible dataclass.
"""

from dataclasses import dataclass
from itertools import pairwise

from hypothesis import strategies as st

from ledger.models.enums import AccountType, EntryDirection

CURRENCIES = ("USD", "EUR")
MAX_AMOUNT = 10_000


@dataclass(frozen=True, slots=True)
class AccountSpec:
    type: AccountType
    currency: str
    allow_negative: bool


@dataclass(frozen=True, slots=True)
class Leg:
    account_index: int
    direction: EntryDirection
    amount: int


@dataclass(frozen=True, slots=True)
class PostOp:
    legs: tuple[Leg, ...]
    currency: str


@dataclass(frozen=True, slots=True)
class ReverseOp:
    #: Resolved modulo the number of successfully-posted transactions at
    #: execution time, so the shrinker can delete earlier PostOps freely.
    target_index: int


Op = PostOp | ReverseOp
Scenario = tuple[tuple[AccountSpec, ...], tuple[Op, ...]]


@st.composite
def account_specs(draw: st.DrawFn, min_size: int = 2, max_size: int = 5) -> tuple[AccountSpec, ...]:
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    specs = [
        AccountSpec(
            type=draw(st.sampled_from(list(AccountType))),
            currency=draw(st.sampled_from(CURRENCIES)),
            allow_negative=draw(st.booleans()),
        )
        for _ in range(n)
    ]
    # Guarantee at least two accounts share a currency, or nearly every
    # generated posting degenerates into a trivial CurrencyMismatch and the
    # test stops exercising anything else.
    specs[1] = AccountSpec(
        type=specs[1].type, currency=specs[0].currency, allow_negative=specs[1].allow_negative
    )
    return tuple(specs)


@st.composite
def balanced_legs(draw: st.DrawFn, n_accounts: int, currency: str) -> tuple[Leg, ...]:
    """Construct (never filter-and-hope) a balanced set of 2-4 legs against
    accounts of one currency. `total` is drawn before `k` so `k` can be
    bounded by how many >=1 debit legs `total` can actually support --
    avoids ever asking Hypothesis for an empty integer range."""
    total = draw(st.integers(min_value=1, max_value=MAX_AMOUNT))
    max_k = min(4, n_accounts, total + 1)
    k = draw(st.integers(min_value=2, max_value=max_k))
    indices = draw(
        st.lists(
            st.integers(min_value=0, max_value=n_accounts - 1),
            min_size=k,
            max_size=k,
            unique=True,
        )
    )
    # Split `total` across k-1 debit legs (each >= 1); last leg is the sole
    # credit for the full `total`.
    if k == 2:
        debit_amounts = [total]
    else:
        # Split points must be distinct -- a repeated split would produce a
        # zero-length (amount=0) leg, which post_transaction rejects.
        splits = draw(
            st.lists(
                st.integers(min_value=1, max_value=total - 1),
                min_size=k - 2,
                max_size=k - 2,
                unique=True,
            )
        )
        splits.sort()
        bounds = [0, *splits, total]
        debit_amounts = [b - a for a, b in pairwise(bounds)]

    legs = [
        Leg(account_index=indices[i], direction=EntryDirection.DEBIT, amount=debit_amounts[i])
        for i in range(k - 1)
    ]
    legs.append(Leg(account_index=indices[k - 1], direction=EntryDirection.CREDIT, amount=total))
    return tuple(legs)


@st.composite
def post_ops(draw: st.DrawFn, accounts: tuple[AccountSpec, ...]) -> PostOp:
    currency = draw(st.sampled_from(CURRENCIES))
    eligible = [i for i, a in enumerate(accounts) if a.currency == currency]
    if len(eligible) < 2:
        currency = accounts[0].currency
        eligible = [i for i, a in enumerate(accounts) if a.currency == currency]
    n = len(eligible)
    raw_legs = draw(balanced_legs(n_accounts=n, currency=currency))
    legs = tuple(
        Leg(account_index=eligible[leg.account_index], direction=leg.direction, amount=leg.amount)
        for leg in raw_legs
    )
    return PostOp(legs=legs, currency=currency)


@st.composite
def reverse_ops(draw: st.DrawFn) -> ReverseOp:
    return ReverseOp(target_index=draw(st.integers(min_value=0, max_value=1_000_000)))


@st.composite
def scenarios(draw: st.DrawFn, min_ops: int = 1, max_ops: int = 15) -> Scenario:
    accounts = draw(account_specs())
    n_ops = draw(st.integers(min_value=min_ops, max_value=max_ops))
    ops: list[Op] = []
    for _ in range(n_ops):
        if draw(st.booleans()) or not ops:
            ops.append(draw(post_ops(accounts)))
        else:
            ops.append(draw(reverse_ops()))
    return accounts, tuple(ops)


@st.composite
def unbalanced_legs(draw: st.DrawFn, n_accounts: int, currency: str) -> tuple[Leg, ...]:
    """A deliberately unbalanced 2-leg posting for the negative-path
    property."""
    indices = draw(
        st.lists(
            st.integers(min_value=0, max_value=n_accounts - 1), min_size=2, max_size=2, unique=True
        )
    )
    debit = draw(st.integers(min_value=1, max_value=MAX_AMOUNT))
    delta = draw(st.integers(min_value=1, max_value=MAX_AMOUNT))
    credit = debit + delta
    return (
        Leg(account_index=indices[0], direction=EntryDirection.DEBIT, amount=debit),
        Leg(account_index=indices[1], direction=EntryDirection.CREDIT, amount=credit),
    )
