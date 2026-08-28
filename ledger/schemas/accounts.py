import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from ledger.models.enums import AccountType, EntryDirection
from ledger.schemas.common import CurrencyCode, MinorUnits, SignedMinorUnits

AccountName = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AccountName
    type: AccountType
    currency: CurrencyCode
    # Safe default: an account cannot go negative unless a caller explicitly
    # asks for it. The DB column is NOT NULL with no server default, so this
    # schema default is the only source of the default value.
    allow_negative: bool = False
    is_suspense: bool = False


class AccountRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name: str
    type: AccountType
    currency: CurrencyCode
    allow_negative: bool
    is_suspense: bool
    created_at: datetime
    balance: SignedMinorUnits
    entry_count: int


class EntryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    transaction_id: uuid.UUID
    account_id: uuid.UUID
    direction: EntryDirection
    amount: MinorUnits
    currency: CurrencyCode
    created_at: datetime
