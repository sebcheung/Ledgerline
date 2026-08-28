"""Shared Pydantic v2 field types.

Every model in `ledger.schemas` uses `extra="forbid"` -- a typo'd field in a
money API must not be silently ignored -- and `from_attributes=True` so
response models can be built directly from the frozen dataclasses returned
by `ledger.core.posting` and from SQLAlchemy `Row` objects.

Cross-entry semantics (single currency across a transaction, debits ==
credits, account existence, sufficient funds) are deliberately *not*
expressed as Pydantic validators here -- see `ledger.core` for why. Pydantic
is responsible only for field-shaped validation.
"""

from typing import Annotated

from pydantic import Field, StringConstraints

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$", min_length=3, max_length=3)]

#: Entry amounts: must be strictly positive (SPEC.md §3 `CHECK (amount > 0)`)
#: and within the bigint domain.
MinorUnits = Annotated[int, Field(gt=0, lt=2**63)]

#: Balances/deltas may be negative or zero.
SignedMinorUnits = Annotated[int, Field(gt=-(2**63), lt=2**63)]
