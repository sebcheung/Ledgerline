"""Ledger posting and reversal (SPEC.md §5).

Concurrency model: Postgres default READ COMMITTED, with an explicit
`SELECT ... FOR UPDATE OF account_balances`, rows acquired in ascending
`account_id` order. Postgres places the `LockRows` plan node *above* `Sort`,
so locks are actually taken in `ORDER BY` order -- this is the fact that
makes ordered acquisition real and makes deadlock between two concurrent
postings structurally impossible, regardless of the order their callers
named accounts in.

Posting uses SQLAlchemy Core (`insert()`/`select()`/`update()` with
`RETURNING`), not the ORM unit of work: the models carry no
`relationship()`s, and a `SAVEPOINT` rollback (used to survive the
idempotency-key unique-violation backstop) leaves no pending ORM instances
behind to accidentally get re-flushed later, because there are none.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import BigInteger, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.core.errors import (
    AccountNotFound,
    AlreadyReversed,
    CurrencyMismatch,
    DuplicateTransaction,
    InsufficientFunds,
    InvalidTransactionShape,
    TransactionNotFound,
)
from ledger.core.invariants import assert_transaction_balanced, signed_delta
from ledger.db.errors import constraint_name_of
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.entries import Entry
from ledger.models.enums import AccountType, EntryDirection, TransactionSource, TransactionStatus
from ledger.models.transactions import Transaction
from ledger.webhooks.outbox import (
    EVENT_TRANSACTION_POSTED,
    EVENT_TRANSACTION_REVERSED,
    emit_event,
    transaction_event_payload,
)

logger = logging.getLogger(__name__)

_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1

#: Constraint names from migrations/versions/0001_initial_schema.py.
_CONSTRAINT_IDEMPOTENCY_KEY = "transactions_idempotency_key_key"
_CONSTRAINT_REVERSAL_OF = "uq_transactions_reversal_of"

LockHook = Callable[[uuid.UUID], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """One leg of a transaction being posted."""

    account_id: uuid.UUID
    direction: EntryDirection
    amount: int
    currency: str


@dataclass(frozen=True, slots=True)
class PostedEntry:
    id: uuid.UUID
    transaction_id: uuid.UUID
    account_id: uuid.UUID
    direction: EntryDirection
    amount: int
    currency: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PostedTransaction:
    """What `post_transaction`/`reverse_transaction` return.

    Deliberately not the ORM `Transaction`: posting uses Core inserts, so
    there is no session-attached instance to hand back; the ORM model also
    carries no `entries` relationship, and both the API response and the
    outbox payload need the entries. A frozen DTO serializes directly into
    both.
    """

    id: uuid.UUID
    idempotency_key: str | None
    external_ref: str | None
    description: str | None
    status: TransactionStatus
    reversal_of: uuid.UUID | None
    source: TransactionSource
    created_at: datetime
    entries: tuple[PostedEntry, ...]

    @property
    def currency(self) -> str:
        return self.entries[0].currency

    @property
    def total_debits(self) -> int:
        return sum(e.amount for e in self.entries if e.direction is EntryDirection.DEBIT)


def _validate_shape(entries: Sequence[EntryRequest]) -> None:
    if len(entries) < 2:
        raise InvalidTransactionShape(
            "a transaction requires at least two entries", reason="at_least_two_entries"
        )

    currencies = {e.currency for e in entries}
    if len(currencies) > 1:
        first, *rest = sorted(currencies)
        raise CurrencyMismatch(
            "all entries in one transaction must share one currency",
            expected=first,
            actual=rest[0],
        )

    for e in entries:
        if not (0 < e.amount <= _BIGINT_MAX):
            raise InvalidTransactionShape(
                f"entry amount must be a positive value within the bigint domain, got {e.amount}",
                reason="amount_out_of_range",
            )

    assert_transaction_balanced(entries)


_LockRow = tuple[uuid.UUID, int, str, str, AccountType, bool]


async def _lock_account_balances(
    session: AsyncSession, account_ids: list[uuid.UUID], *, _lock_hook: LockHook | None
) -> list[_LockRow]:
    """Steps 2-3 of SPEC.md §5: lock the distinct `account_balances` rows in
    ascending `account_id` order, joined to `accounts` for currency/type/
    allow_negative. Self-heals a missing (but real) balance row; raises
    AccountNotFound for a genuinely missing account.

    Returns tuples of
    (account_id, balance, balance_currency, account_currency, account_type, allow_negative).
    """
    lock_stmt = (
        select(
            AccountBalance.account_id,
            AccountBalance.balance,
            AccountBalance.currency,
            Account.currency,
            Account.type,
            Account.allow_negative,
        )
        # Inner join is mandatory: Postgres refuses FOR UPDATE on the
        # nullable side of an outer join.
        .join(Account, Account.id == AccountBalance.account_id)
        .where(AccountBalance.account_id.in_(account_ids))
        .order_by(AccountBalance.account_id)
        # FOR UPDATE OF account_balances only -- locking `accounts` too
        # would serialize unrelated work and introduce a second lock
        # ordering for no benefit (accounts is read-mostly).
        .with_for_update(of=AccountBalance)
    )
    rows = (await session.execute(lock_stmt)).all()

    if _lock_hook is not None:
        for row in rows:
            await _lock_hook(row[0])

    if len(rows) < len(account_ids):
        found_ids = {row[0] for row in rows}
        missing_ids = [aid for aid in account_ids if aid not in found_ids]
        existing = (
            (await session.execute(select(Account.id).where(Account.id.in_(missing_ids))))
            .scalars()
            .all()
        )
        existing_set = set(existing)
        absent = [aid for aid in missing_ids if aid not in existing_set]
        if absent:
            raise AccountNotFound(
                f"account(s) not found: {', '.join(str(a) for a in absent)}",
                account_ids=absent,
            )

        # Accounts exist but have no account_balances row -- this should be
        # unreachable in practice, because POST /v1/accounts creates both
        # rows atomically. Self-heal and log loudly so an operator notices
        # the anomaly if it ever fires.
        backfill_ids = [aid for aid in missing_ids if aid in existing_set]
        logger.warning(
            "backfilling missing account_balances rows",
            extra={"account_ids": [str(a) for a in backfill_ids]},
        )
        backfill_select = (
            select(
                Account.id,
                Account.currency,
                literal(0, type_=BigInteger),
                literal(0, type_=BigInteger),
            )
            .where(Account.id.in_(backfill_ids))
            .order_by(Account.id)
        )
        backfill_stmt = (
            pg_insert(AccountBalance)
            .from_select(["account_id", "currency", "balance", "entry_count"], backfill_select)
            .on_conflict_do_nothing(index_elements=[AccountBalance.account_id])
        )
        await session.execute(backfill_stmt)
        rows = (await session.execute(lock_stmt)).all()
        if len(rows) < len(account_ids):
            raise AccountNotFound("account balance backfill did not converge")

    return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


async def post_transaction(
    session: AsyncSession,
    entries: Sequence[EntryRequest],
    *,
    idempotency_key: str | None = None,
    external_ref: str | None = None,
    description: str | None = None,
    source: TransactionSource = TransactionSource.API,
    reversal_of: uuid.UUID | None = None,
    _lock_hook: LockHook | None = None,
) -> PostedTransaction:
    """Post a balanced, multi-leg transaction. Never commits -- the caller
    owns the transaction boundary (SPEC.md §13).

    `reversal_of` is not in SPEC.md §5's literal signature block, but the
    Reversal pseudocode (step 3) requires passing it through; added as a
    keyword-only parameter. `_lock_hook`, also not in the spec, is a private
    test seam invoked once per locked account id, immediately after the
    lock is acquired -- it is what lets the deadlock-order test inject a
    delay to widen the window in which an incorrectly-ordered
    implementation would deadlock.
    """
    # Step 1: shape validation.
    _validate_shape(entries)
    txn_currency = entries[0].currency

    # Step 2: distinct account ids, sorted ascending.
    account_ids = sorted({e.account_id for e in entries})

    # Step 3: ordered lock.
    rows = await _lock_account_balances(session, account_ids, _lock_hook=_lock_hook)

    deltas: dict[uuid.UUID, int] = {aid: 0 for aid in account_ids}
    counts: dict[uuid.UUID, int] = {aid: 0 for aid in account_ids}
    types_by_id: dict[uuid.UUID, tuple[AccountType, bool, int]] = {}
    for (
        account_id,
        balance,
        balance_currency,
        account_currency,
        account_type,
        allow_negative,
    ) in rows:
        # Step 3b: currency consistency (invariant 4).
        if account_currency != txn_currency:
            raise CurrencyMismatch(
                f"account {account_id} is {account_currency}, transaction is {txn_currency}",
                expected=txn_currency,
                actual=account_currency,
                account_id=account_id,
            )
        if balance_currency != account_currency:
            # Data-integrity bug, not a client error -- account_balances is
            # supposed to be kept in lockstep with accounts.currency at
            # account-creation time and never diverge.
            logger.error(
                "account_balances.currency diverged from accounts.currency",
                extra={"account_id": str(account_id)},
            )
            raise CurrencyMismatch(
                f"account {account_id} balance currency {balance_currency} != "
                f"account currency {account_currency}",
                expected=account_currency,
                actual=balance_currency,
                account_id=account_id,
            )
        types_by_id[account_id] = (account_type, allow_negative, balance)

    # Step 4: per-account deltas and entry counts.
    for e in entries:
        account_type, _allow_negative, _balance = types_by_id[e.account_id]
        deltas[e.account_id] += signed_delta(account_type, e.direction, e.amount)
        counts[e.account_id] += 1

    # Step 5: sign constraint (invariant 5), against the *locked* balance.
    for account_id, delta in deltas.items():
        account_type, allow_negative, current_balance = types_by_id[account_id]
        resulting = current_balance + delta
        if not (_BIGINT_MIN <= resulting <= _BIGINT_MAX):
            raise InvalidTransactionShape(
                f"posting would move account {account_id} outside the bigint domain",
                reason="balance_overflow",
                account_id=account_id,
            )
        if not allow_negative and resulting < 0:
            raise InsufficientFunds(
                f"account {account_id} would go negative ({resulting})",
                account_id=account_id,
                currency=txn_currency,
                current_balance=current_balance,
                attempted_delta=delta,
                resulting_balance=resulting,
            )

    # Step 6: insert the transaction row, inside a SAVEPOINT so the
    # idempotency-key/reversal-of unique-violation backstop can fire without
    # poisoning the outer transaction -- the caller (Phase 3's idempotency
    # layer, Phase 4's resolver loop) needs to keep using this session
    # afterwards.
    try:
        async with session.begin_nested():
            txn_stmt = (
                insert(Transaction)
                .values(
                    idempotency_key=idempotency_key,
                    external_ref=external_ref,
                    description=description,
                    status=TransactionStatus.POSTED,
                    reversal_of=reversal_of,
                    source=source,
                )
                .returning(Transaction.id, Transaction.created_at, Transaction.status)
            )
            txn_row = (await session.execute(txn_stmt)).one()
    except IntegrityError as exc:
        constraint = constraint_name_of(exc)
        if constraint == _CONSTRAINT_IDEMPOTENCY_KEY:
            raise DuplicateTransaction(
                f"idempotency key {idempotency_key!r} already used",
                idempotency_key=idempotency_key,
            ) from exc
        if constraint == _CONSTRAINT_REVERSAL_OF:
            raise AlreadyReversed(
                f"transaction {reversal_of} already has a reversal",
                transaction_id=reversal_of,
            ) from exc
        raise

    transaction_id = txn_row.id

    # Step 7: insert entries, one multi-row INSERT. Build PostedEntry from
    # the RETURNING rows themselves -- multi-row RETURNING order is not
    # guaranteed by Postgres, so zipping against the input list would be
    # unsafe.
    entry_stmt = (
        insert(Entry)
        .values(
            [
                {
                    "transaction_id": transaction_id,
                    "account_id": e.account_id,
                    "direction": e.direction,
                    "amount": e.amount,
                    "currency": e.currency,
                }
                for e in entries
            ]
        )
        .returning(
            Entry.id,
            Entry.account_id,
            Entry.direction,
            Entry.amount,
            Entry.currency,
            Entry.created_at,
        )
    )
    entry_rows = (await session.execute(entry_stmt)).all()
    posted_entries = tuple(
        PostedEntry(
            id=r.id,
            transaction_id=transaction_id,
            account_id=r.account_id,
            direction=r.direction,
            amount=r.amount,
            currency=r.currency,
            created_at=r.created_at,
        )
        for r in entry_rows
    )

    # Step 8: update balances. SQL-side `balance = balance + delta` so the
    # write is correct even under a hypothetically stale Python-side read --
    # it can't be stale here (the row is locked), but this form survives
    # future refactors. `updated_at` is supplied by the column's
    # `onupdate=text("now()")`; do not set it explicitly.
    for account_id in sorted(deltas):
        await session.execute(
            update(AccountBalance)
            .where(AccountBalance.account_id == account_id)
            .values(
                balance=AccountBalance.balance + deltas[account_id],
                entry_count=AccountBalance.entry_count + counts[account_id],
            )
        )

    # Step 9: outbox event, in the same DB transaction as the ledger write.
    posted = PostedTransaction(
        id=transaction_id,
        idempotency_key=idempotency_key,
        external_ref=external_ref,
        description=description,
        status=txn_row.status,
        reversal_of=reversal_of,
        source=source,
        created_at=txn_row.created_at,
        entries=posted_entries,
    )
    await emit_event(session, EVENT_TRANSACTION_POSTED, transaction_event_payload(posted))

    logger.info(
        "transaction.posted",
        extra={
            "transaction_id": str(transaction_id),
            "currency": txn_currency,
            "entry_count": len(entries),
            "idempotency_key": idempotency_key,
        },
    )
    return posted


async def reverse_transaction(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
    source: TransactionSource = TransactionSource.API,
) -> PostedTransaction:
    """Reverse a posted transaction with the exact negation of its entries.

    `source` is parameterized (SPEC.md §5's pseudocode hardcodes
    `source='api'`, but SPEC.md §7 requires reconciliation-initiated
    reversals to carry `source='reconciliation'`); the default preserves
    §5's text.

    Three independent guards prevent a transaction from being reversed
    twice: the `SELECT ... FOR UPDATE` below, the status-compare-and-swap
    in step 4, and the DB's `uq_transactions_reversal_of` partial unique
    index.

    The `transactions` row lock taken here is a different lock family from
    the `account_balances` locks taken inside `post_transaction`, and is
    always acquired first, before any balance lock. Two concurrent
    reversals lock distinct transaction rows; a plain posting never locks a
    transaction row at all -- so this ordering cannot introduce a new
    deadlock cycle with either plain postings or other reversals.
    """
    orig_stmt = (
        select(Transaction.id, Transaction.status)
        .where(Transaction.id == transaction_id)
        .with_for_update()
    )
    orig = (await session.execute(orig_stmt)).one_or_none()
    if orig is None:
        raise TransactionNotFound(
            f"transaction {transaction_id} not found", transaction_id=transaction_id
        )
    if orig.status is TransactionStatus.REVERSED:
        existing = (
            await session.execute(
                select(Transaction.id).where(Transaction.reversal_of == transaction_id)
            )
        ).scalar_one_or_none()
        raise AlreadyReversed(
            f"transaction {transaction_id} is already reversed",
            transaction_id=transaction_id,
            reversal_transaction_id=existing,
        )

    entry_rows = (
        await session.execute(
            select(Entry.account_id, Entry.direction, Entry.amount, Entry.currency)
            .where(Entry.transaction_id == transaction_id)
            .order_by(Entry.created_at, Entry.id)
        )
    ).all()

    mirrored = [
        EntryRequest(
            account_id=r.account_id,
            direction=(
                EntryDirection.CREDIT
                if r.direction is EntryDirection.DEBIT
                else EntryDirection.DEBIT
            ),
            amount=r.amount,
            currency=r.currency,
        )
        for r in entry_rows
    ]

    posted = await post_transaction(
        session,
        mirrored,
        idempotency_key=idempotency_key,
        # Deliberately NULL: carrying the original's external_ref would
        # make Phase 4's pass-1 exact matcher see two ledger transactions
        # for one settlement line.
        external_ref=None,
        description=f"Reversal of {transaction_id}",
        source=source,
        reversal_of=transaction_id,
    )

    cas_result = cast(
        CursorResult[Any],
        await session.execute(
            update(Transaction)
            .where(Transaction.id == transaction_id, Transaction.status == TransactionStatus.POSTED)
            .values(status=TransactionStatus.REVERSED)
        ),
    )
    if cas_result.rowcount != 1:
        # Someone else won the compare-and-swap between our initial lock
        # and here -- impossible under the FOR UPDATE above without a bug
        # elsewhere, but treated as the same domain error rather than an
        # assertion failure.
        raise AlreadyReversed(
            f"transaction {transaction_id} was reversed concurrently",
            transaction_id=transaction_id,
        )

    await emit_event(
        session,
        EVENT_TRANSACTION_REVERSED,
        {
            "transaction_id": str(transaction_id),
            "reversal_transaction_id": str(posted.id),
        },
    )

    return posted
