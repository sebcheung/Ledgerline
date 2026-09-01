"""Resolution policy for newly-created findings (SPEC.md §7 `resolver.py`).

Only ever called by `ledger.reconciliation.runner` with the rows the
findings `INSERT ... ON CONFLICT DO NOTHING RETURNING ...` actually
returned -- never with the matcher's full in-memory classification. That
is what makes every resolution here safe to run exactly once per finding,
forever (see docs/DECISIONS.md Phase 4).

Every adjustment goes through `post_transaction`, wrapped in its own
`session.begin_nested()`: `posting.py`'s own SAVEPOINT (around the
`INSERT INTO transactions` statement only) does not cover the pre-flight
raises (`InsufficientFunds`, `CurrencyMismatch`, `AccountNotFound`,
`InvalidTransactionShape`) this loop will actually hit, nor the
`IntegrityError`s an entries-insert or outbox-emit failure would leave the
outer transaction poisoned by. One expected failure here must not fail
every later finding in the same run.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.core.errors import AccountNotFound, InvalidFindingResolution, LedgerError
from ledger.core.posting import EntryRequest, PostedTransaction, post_transaction
from ledger.models.accounts import Account
from ledger.models.entries import Entry
from ledger.models.enums import (
    AccountType,
    EntryDirection,
    ReconciliationFindingType,
    ReconciliationResolution,
    TransactionSource,
)
from ledger.models.reconciliation import ReconciliationFinding
from ledger.models.settlements import SettlementLine

logger = logging.getLogger(__name__)


async def _find_account(
    session: AsyncSession, *, currency: str, is_clearing: bool
) -> uuid.UUID | None:
    column = Account.is_clearing if is_clearing else Account.is_suspense
    return (
        await session.execute(
            select(Account.id).where(Account.currency == currency, column.is_(True))
        )
    ).scalar_one_or_none()


async def _select_asset_leg(
    session: AsyncSession, transaction_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    """The transaction's single non-clearing asset-type leg, if there is
    exactly one; otherwise fall back to the currency's clearing account --
    an asset-to-asset transfer (two such legs) is the common case a
    single-leg rule would never auto-resolve."""
    currency = (
        await session.execute(
            select(Entry.currency).where(Entry.transaction_id == transaction_id).limit(1)
        )
    ).scalar_one()
    account_ids = (
        (
            await session.execute(
                select(Entry.account_id)
                .join(Account, Account.id == Entry.account_id)
                .where(
                    Entry.transaction_id == transaction_id,
                    Account.type == AccountType.ASSET,
                    Account.is_clearing.is_(False),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if len(account_ids) == 1:
        return currency, account_ids[0]
    fallback = await _find_account(session, currency=currency, is_clearing=True)
    return currency, fallback


async def _adjust(
    session: AsyncSession, *, entries: list[EntryRequest], finding_id: uuid.UUID, description: str
) -> PostedTransaction | None:
    try:
        async with session.begin_nested():
            return await post_transaction(
                session,
                entries,
                # Deterministic key, defense-in-depth on top of the
                # unconditional findings index: even if a future code path
                # ever drove the resolver off the wrong row set, the
                # transactions.idempotency_key unique constraint stops a
                # second adjustment for this finding from posting at all.
                idempotency_key=f"recon-adjust:{finding_id}",
                external_ref=None,
                description=description,
                source=TransactionSource.RECONCILIATION,
            )
    except LedgerError as exc:
        logger.warning(
            "reconciliation.unresolved",
            extra={
                "finding_id": str(finding_id),
                "reason": exc.error_type,
                "detail": exc.detail,
            },
        )
        return None


async def _finalize(
    session: AsyncSession,
    finding_id: uuid.UUID,
    *,
    resolution: ReconciliationResolution,
    resolving_transaction_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        "resolution": resolution,
        "resolving_transaction_id": resolving_transaction_id,
        "resolved_at": text("now()"),
    }
    if detail is not None:
        values["detail"] = detail
    await session.execute(
        update(ReconciliationFinding).where(ReconciliationFinding.id == finding_id).values(**values)
    )
    logger.info(
        "reconciliation.auto_resolved"
        if resolution != ReconciliationResolution.UNRESOLVED
        else "reconciliation.unresolved",
        extra={"finding_id": str(finding_id), "resolution": resolution.value},
    )


async def _resolve_unexpected_settlement(session: AsyncSession, row: Row[Any]) -> None:
    threshold = get_settings().recon_auto_resolve_threshold_minor
    line = (
        await session.execute(
            select(SettlementLine.amount, SettlementLine.currency).where(
                SettlementLine.id == row.settlement_line_id
            )
        )
    ).one()
    if line.amount == 0 or abs(line.amount) > threshold:
        return  # leave unresolved -- above threshold or nothing to move

    clearing_id = await _find_account(session, currency=line.currency, is_clearing=True)
    suspense_id = await _find_account(session, currency=line.currency, is_clearing=False)
    if clearing_id is None or suspense_id is None:
        logger.warning(
            "reconciliation.unresolved",
            extra={
                "finding_id": str(row.id),
                "reason": "missing_clearing_or_suspense_account",
                "currency": line.currency,
            },
        )
        return

    magnitude = abs(line.amount)
    if line.amount > 0:
        entries = [
            EntryRequest(clearing_id, EntryDirection.DEBIT, magnitude, line.currency),
            EntryRequest(suspense_id, EntryDirection.CREDIT, magnitude, line.currency),
        ]
    else:
        entries = [
            EntryRequest(suspense_id, EntryDirection.DEBIT, magnitude, line.currency),
            EntryRequest(clearing_id, EntryDirection.CREDIT, magnitude, line.currency),
        ]
    posted = await _adjust(
        session,
        entries=entries,
        finding_id=row.id,
        description=f"Reconciliation adjustment for finding {row.id}",
    )
    if posted is None:
        return
    await session.execute(
        update(SettlementLine)
        .where(SettlementLine.id == row.settlement_line_id)
        .values(matched_transaction_id=posted.id)
    )
    await _finalize(
        session,
        row.id,
        resolution=ReconciliationResolution.AUTO_RESOLVED,
        resolving_transaction_id=posted.id,
    )


async def _resolve_amount_mismatch(session: AsyncSession, row: Row[Any]) -> None:
    threshold = get_settings().recon_auto_resolve_threshold_minor
    delta = row.delta_amount
    if delta is None or delta == 0 or abs(delta) > threshold:
        return

    currency, asset_account_id = await _select_asset_leg(session, row.transaction_id)
    if asset_account_id is None:
        logger.warning(
            "reconciliation.unresolved",
            extra={
                "finding_id": str(row.id),
                "reason": "no_asset_leg_and_no_clearing_account",
                "currency": currency,
            },
        )
        return
    suspense_id = await _find_account(session, currency=currency, is_clearing=False)
    if suspense_id is None:
        logger.warning(
            "reconciliation.unresolved",
            extra={
                "finding_id": str(row.id),
                "reason": "missing_suspense_account",
                "currency": currency,
            },
        )
        return

    magnitude = abs(delta)
    if delta > 0:
        entries = [
            EntryRequest(asset_account_id, EntryDirection.DEBIT, magnitude, currency),
            EntryRequest(suspense_id, EntryDirection.CREDIT, magnitude, currency),
        ]
    else:
        entries = [
            EntryRequest(suspense_id, EntryDirection.DEBIT, magnitude, currency),
            EntryRequest(asset_account_id, EntryDirection.CREDIT, magnitude, currency),
        ]
    posted = await _adjust(
        session,
        entries=entries,
        finding_id=row.id,
        description=f"Reconciliation adjustment for finding {row.id}",
    )
    if posted is None:
        return
    await _finalize(
        session,
        row.id,
        resolution=ReconciliationResolution.AUTO_RESOLVED,
        resolving_transaction_id=posted.id,
    )


async def resolve(session: AsyncSession, created: Sequence[Row[Any]]) -> dict[str, int]:
    """`created` is the `RETURNING` set of the findings insert -- rows this
    run actually created, each carrying at least `id`, `finding_type`,
    `transaction_id`, `settlement_line_id`, `delta_amount`. Returns a count
    of newly-created findings by type (the "created" half of
    `findings_by_type`; the caller supplies "observed" separately)."""
    counts: dict[str, int] = defaultdict(int)
    for row in created:
        counts[row.finding_type.value] += 1
        if row.finding_type is ReconciliationFindingType.IN_FLIGHT:
            await _finalize(session, row.id, resolution=ReconciliationResolution.SUPPRESSED)
        elif row.finding_type is ReconciliationFindingType.DUPLICATE_SETTLEMENT:
            await _finalize(session, row.id, resolution=ReconciliationResolution.AUTO_RESOLVED)
        elif row.finding_type is ReconciliationFindingType.UNEXPECTED_SETTLEMENT:
            await _resolve_unexpected_settlement(session, row)
        elif row.finding_type is ReconciliationFindingType.AMOUNT_MISMATCH:
            await _resolve_amount_mismatch(session, row)
        # MISSING_SETTLEMENT, CURRENCY_MISMATCH: stay `unresolved` -- the
        # ledger is authoritative / always manual, respectively.
    return dict(counts)


#: Finding types `POST /v1/reconciliation/findings/{id}/resolve` can post an
#: adjustment for. `in_flight`/`duplicate_settlement` already resolved
#: themselves; `missing_settlement`/`currency_mismatch` have no meaningful
#: delta to move.
ADJUSTABLE_FINDING_TYPES = frozenset(
    {ReconciliationFindingType.UNEXPECTED_SETTLEMENT, ReconciliationFindingType.AMOUNT_MISMATCH}
)


async def resolve_manual_adjustment(
    session: AsyncSession,
    row: Row[Any],
) -> uuid.UUID:
    """The `post_adjustment` action of `POST .../findings/{id}/resolve`:
    the same ledger effect `resolve()` would have posted automatically, but
    bypassing the auto-resolve threshold (an operator has already decided
    this adjustment should happen) and letting a `LedgerError` propagate
    to the caller instead of being swallowed into `unresolved` -- an
    explicit operator action that fails should come back as a typed HTTP
    error, not a silent no-op. Caller (the route) has already done the
    `SELECT ... FOR UPDATE` + compare-and-swap that makes this safe to call
    at most once per finding; the deterministic `recon-adjust:{finding_id}`
    idempotency key is still there as a second line of defense."""
    if row.finding_type not in ADJUSTABLE_FINDING_TYPES:
        raise InvalidFindingResolution(
            f"finding type {row.finding_type.value!r} has nothing to adjust", finding_id=row.id
        )

    if row.finding_type is ReconciliationFindingType.UNEXPECTED_SETTLEMENT:
        line = (
            await session.execute(
                select(SettlementLine.amount, SettlementLine.currency).where(
                    SettlementLine.id == row.settlement_line_id
                )
            )
        ).one()
        clearing_id = await _find_account(session, currency=line.currency, is_clearing=True)
        suspense_id = await _find_account(session, currency=line.currency, is_clearing=False)
        _require_accounts(clearing_id, suspense_id, currency=line.currency)
        assert clearing_id is not None
        assert suspense_id is not None
        magnitude = abs(line.amount)
        currency = line.currency
        if line.amount > 0:
            entries = [
                EntryRequest(clearing_id, EntryDirection.DEBIT, magnitude, currency),
                EntryRequest(suspense_id, EntryDirection.CREDIT, magnitude, currency),
            ]
        else:
            entries = [
                EntryRequest(suspense_id, EntryDirection.DEBIT, magnitude, currency),
                EntryRequest(clearing_id, EntryDirection.CREDIT, magnitude, currency),
            ]
    else:  # AMOUNT_MISMATCH
        delta = row.delta_amount or 0
        currency, asset_account_id = await _select_asset_leg(session, row.transaction_id)
        suspense_id = await _find_account(session, currency=currency, is_clearing=False)
        _require_accounts(asset_account_id, suspense_id, currency=currency)
        assert asset_account_id is not None
        assert suspense_id is not None
        magnitude = abs(delta)
        if delta > 0:
            entries = [
                EntryRequest(asset_account_id, EntryDirection.DEBIT, magnitude, currency),
                EntryRequest(suspense_id, EntryDirection.CREDIT, magnitude, currency),
            ]
        else:
            entries = [
                EntryRequest(suspense_id, EntryDirection.DEBIT, magnitude, currency),
                EntryRequest(asset_account_id, EntryDirection.CREDIT, magnitude, currency),
            ]

    posted = await post_transaction(
        session,
        entries,
        idempotency_key=f"recon-adjust:{row.id}",
        external_ref=None,
        description=f"Manually-resolved reconciliation adjustment for finding {row.id}",
        source=TransactionSource.RECONCILIATION,
    )
    if row.finding_type is ReconciliationFindingType.UNEXPECTED_SETTLEMENT:
        await session.execute(
            update(SettlementLine)
            .where(SettlementLine.id == row.settlement_line_id)
            .values(matched_transaction_id=posted.id)
        )
    return posted.id


def _require_accounts(*account_ids: uuid.UUID | None, currency: str) -> None:
    if any(a is None for a in account_ids):
        raise AccountNotFound(
            f"no clearing/suspense account configured for {currency}", currency=currency
        )
