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
from collections.abc import Mapping
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

    #: Extra HTTP response headers this error type always sets (e.g.
    #: `Retry-After`). Empty by default -- a method rather than a bare
    #: ClassVar read so a subclass (e.g. Phase 7's rate limiter) can compute
    #: a dynamic value without changing the call site.
    headers: ClassVar[Mapping[str, str]] = {}

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra: dict[str, Any] = {k: _jsonable(v) for k, v in extra.items()}

    def as_problem_members(self) -> dict[str, Any]:
        """JSON-safe extension members for the RFC 7807 problem document."""
        return dict(self.extra)

    def problem_headers(self) -> dict[str, str]:
        """Extra HTTP headers to attach to the RFC 7807 response."""
        return dict(self.headers)


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
    fires -- either because a caller bypassed the idempotency layer
    entirely, or because Phase 3's `run()` reclaimed a stale lock while the
    original request was still in flight and lost the race on the INSERT.
    Deliberately owns `/errors/idempotency-conflict` (and its `Retry-After`
    header) as SPEC.md §9's single 409 idempotency slot -- the in-flight
    lock conflict described in SPEC.md §6 is the same client-facing signal
    (retry), so `ledger.core.idempotency` imports this class under the
    alias `IdempotencyConflict` rather than declaring a second URI. Phase
    3's `run()` intercepts this exception via the session's SAVEPOINT (see
    `posting.py`'s `begin_nested()`) and turns it into a stored-response
    replay before it ever reaches a caller directly; it only surfaces as-is
    to code that writes `idempotency_key` without going through that layer
    (e.g. Phase 4's resolver)."""

    error_type = "/errors/idempotency-conflict"
    title = "Idempotency conflict"
    status = 409
    headers: ClassVar[Mapping[str, str]] = {"Retry-After": "1"}


class IdempotencyKeyReuse(LedgerError):
    """SPEC.md §6: the same `(key, endpoint)` was reused with a request body
    whose canonical fingerprint does not match the one stored for that key
    -- either the original completed with a different body, or a stale lock
    was reclaimed with a different body."""

    error_type = "/errors/idempotency-key-reuse"
    title = "Idempotency key reuse"
    status = 422


class IdempotencyKeyScopeConflict(LedgerError):
    """Extension: SPEC.md §6 names this condition ("if row.endpoint !=
    endpoint") but §9's error table gives it no URI -- the same key was
    presented against a different route than the one it was first claimed
    for."""

    error_type = "/errors/idempotency-key-scope-conflict"
    title = "Idempotency key scope conflict"
    status = 422


class IdempotencyStateError(LedgerError):
    """Extension: the idempotency protocol reached a state SPEC.md §6 does
    not anticipate -- e.g. `complete_key` found no matching `in_progress`
    row to update. This is a server-side invariant violation, not a client
    error; it always fails the enclosing transaction closed."""

    error_type = "/errors/idempotency-state"
    title = "Idempotency state error"
    status = 500


class InvalidRequestBody(LedgerError):
    """Extension: `ledger.core.idempotency.canonical_hash` was asked to
    fingerprint a body that is not valid JSON. Unreachable through the API
    today -- FastAPI's own body parsing rejects a malformed request before
    any dependency runs -- but every caller of `canonical_hash` must have a
    typed failure mode to raise."""

    error_type = "/errors/invalid-request-body"
    title = "Invalid request body"
    status = 400


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


class ReconciliationRunInProgress(LedgerError):
    """Phase 4: `pg_try_advisory_xact_lock` failed to acquire the single
    reconciliation-run lock. A genuinely new 409 slot -- deliberately not a
    reuse of `/errors/idempotency-conflict`, which `DuplicateTransaction`
    owns exclusively (see docs/DECISIONS.md Phase 3)."""

    error_type = "/errors/reconciliation-run-in-progress"
    title = "A reconciliation run is already in progress"
    status = 409
    headers: ClassVar[Mapping[str, str]] = {"Retry-After": "5"}


class ReconciliationRunNotFound(LedgerError):
    error_type = "/errors/reconciliation-run-not-found"
    title = "Reconciliation run not found"
    status = 404


class ReconciliationFindingNotFound(LedgerError):
    error_type = "/errors/reconciliation-finding-not-found"
    title = "Reconciliation finding not found"
    status = 404


class FindingAlreadyResolved(LedgerError):
    """The compare-and-swap `UPDATE ... WHERE resolution = 'unresolved'`
    found no matching row -- the same layered-guards pattern
    `docs/DECISIONS.md` uses for double-reversal, applied to
    `POST /v1/reconciliation/findings/{id}/resolve`, which (unlike the run
    endpoint) is not itself idempotent."""

    error_type = "/errors/finding-already-resolved"
    title = "Finding already resolved"
    status = 409


class InvalidFindingResolution(LedgerError):
    """`post_adjustment` requested against a finding type with nothing to
    adjust (`in_flight`, `duplicate_settlement` -- already self-resolving;
    `missing_settlement`, `currency_mismatch` -- no meaningful delta)."""

    error_type = "/errors/invalid-finding-resolution"
    title = "Invalid finding resolution"
    status = 422


class ClearingAccountExists(LedgerError):
    """The partial unique index `uq_accounts_clearing_per_currency` fired --
    a clearing account for this currency already exists."""

    error_type = "/errors/clearing-account-exists"
    title = "Clearing account already exists"
    status = 409


class ReconciliationRunFailed(LedgerError):
    """SPEC.md §7: "call verify_global_balance() and fail the run loudly if
    it does not hold." A server-side invariant violation, not a client
    error -- raising this rolls the entire run back (see
    ledger.reconciliation.runner)."""

    error_type = "/errors/reconciliation-run-failed"
    title = "Reconciliation run failed post-run verification"
    status = 500
