"""Domain exception hierarchy for the ledger core.

Every failure mode the posting/reversal/invariant code can raise is a
`LedgerError` subclass carrying everything an RFC 7807 problem+json response
needs: a stable `error_type` URI, a short `title`, an HTTP `status`, and a
JSON-safe `extra` mapping of problem-specific detail.

This module must never import from `ledger.api` or `fastapi` -- the
dependency arrow points api -> core, so `ledger.reconciliation` and
`worker` (later phases) can raise and catch these without pulling in the
web framework.
"""

import uuid
from typing import Any, ClassVar


def _jsonable(value: Any) -> Any:
    """Coerce a single extra-detail value into something `json.dumps` (and
    therefore FastAPI's JSONResponse) can serialize without help."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class LedgerError(Exception):
    """Base for every domain error raised by `ledger.core` and its callers."""

    error_type: ClassVar[str]
    title: ClassVar[str]
    status: ClassVar[int]

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra: dict[str, Any] = {k: _jsonable(v) for k, v in extra.items()}

    def as_problem_members(self) -> dict[str, Any]:
        """JSON-safe extension members for the RFC 7807 problem document."""
        return dict(self.extra)


class UnbalancedTransaction(LedgerError):
    """Invariant 1: sum(debits) != sum(credits) for some currency."""

    error_type = "/errors/unbalanced-transaction"
    title = "Unbalanced transaction"
    status = 422


class InsufficientFunds(LedgerError):
    """Invariant 5: posting would drive a non-allow_negative account negative."""

    error_type = "/errors/insufficient-funds"
    title = "Insufficient funds"
    status = 422


class CurrencyMismatch(LedgerError):
    """Invariant 4: an entry's currency does not match its account, or the
    transaction's entries do not all share one currency."""

    error_type = "/errors/currency-mismatch"
    title = "Currency mismatch"
    status = 422


class AccountNotFound(LedgerError):
    error_type = "/errors/account-not-found"
    title = "Account not found"
    status = 404


class TransactionNotFound(LedgerError):
    """Extension: SPEC.md's error table does not name this URI, but the
    reversal path can fail this way and every failure must be typed."""

    error_type = "/errors/transaction-not-found"
    title = "Transaction not found"
    status = 404


class AlreadyReversed(LedgerError):
    error_type = "/errors/already-reversed"
    title = "Transaction already reversed"
    status = 409


class DuplicateTransaction(LedgerError):
    """Raised when the `transactions.idempotency_key` unique constraint
    fires. Deliberately reuses `/errors/idempotency-conflict` rather than a
    new URI -- SPEC.md §9's table already has this slot, and SPEC.md §6
    makes this the layer that turns the backstop into a stored-response
    replay in Phase 3."""

    error_type = "/errors/idempotency-conflict"
    title = "Idempotency conflict"
    status = 409


class InvalidMoney(LedgerError):
    """Extension: raised by `ledger.core.money.Money` for a malformed or
    out-of-range amount."""

    error_type = "/errors/invalid-money"
    title = "Invalid money value"
    status = 422


class InvalidCurrency(LedgerError):
    """Extension: raised by `ledger.core.money.Money` for a malformed
    currency code."""

    error_type = "/errors/invalid-currency"
    title = "Invalid currency code"
    status = 422


class InvalidTransactionShape(LedgerError):
    """Extension: structural transaction problems that are not an
    imbalance -- fewer than two entries, a non-positive amount, an amount
    outside the bigint domain."""

    error_type = "/errors/invalid-transaction"
    title = "Invalid transaction"
    status = 422


class SuspenseAccountExists(LedgerError):
    """Extension: the partial unique index `uq_accounts_suspense_per_currency`
    fired -- a suspense account for this currency already exists."""

    error_type = "/errors/suspense-account-exists"
    title = "Suspense account already exists"
    status = 409


class InvalidCursor(LedgerError):
    """Extension: a pagination cursor failed to decode."""

    error_type = "/errors/invalid-cursor"
    title = "Invalid pagination cursor"
    status = 400
