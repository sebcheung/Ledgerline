"""The three-pass matcher (SPEC.md §7 `matcher.py`).

Framework-free, like `ledger.core`. Classification happens in Python over
two small in-memory candidate lists (a reconciliation window is not the
whole ledger); the resulting `settlement_lines.matched_transaction_id`
updates are written immediately (so a later pass, or a later run, sees
them), but finding rows are *not* inserted here -- `ledger.reconciliation.
runner` owns the `INSERT ... ON CONFLICT DO NOTHING RETURNING` step, and
`ledger.reconciliation.resolver` only ever processes the rows that insert
actually returns. See docs/DECISIONS.md Phase 4 for why driving off
`RETURNING` (not this module's full classification) is what prevents a
recurring finding from being adjusted twice.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, case, cast, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.entries import Entry
from ledger.models.enums import (
    EntryDirection,
    ReconciliationFindingType,
    TransactionSource,
    TransactionStatus,
)
from ledger.models.settlements import SettlementLine
from ledger.models.transactions import Transaction


@dataclass(frozen=True, slots=True)
class ProposedFinding:
    finding_type: ReconciliationFindingType
    transaction_id: uuid.UUID | None
    settlement_line_id: uuid.UUID | None
    delta_amount: int | None
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MatchResult:
    findings: tuple[ProposedFinding, ...]
    matched_count: int


@dataclass(frozen=True, slots=True)
class _TxnCandidate:
    id: uuid.UUID
    external_ref: str | None
    created_at: datetime
    currency: str
    amount: int


@dataclass(frozen=True, slots=True)
class _LineCandidate:
    id: uuid.UUID
    external_ref: str | None
    amount: int
    currency: str
    value_date: date


async def _load_txn_candidates(
    session: AsyncSession, *, window_start: datetime, window_end: datetime
) -> list[_TxnCandidate]:
    """SPEC.md §7's transaction candidate set, plus a Phase 4 addition:
    `reversal_of IS NULL`. A reversal deliberately carries `external_ref =
    NULL` (Phase 2's decision, made for exactly this reason) and has no
    settlement of its own -- without excluding it here, every reversal
    would be a spurious `missing_settlement`. Amount for matching is
    `sum(debits)`, equal to `sum(credits)` by invariant 1."""
    amount_subq = (
        select(
            Entry.transaction_id,
            cast(
                func.sum(case((Entry.direction == EntryDirection.DEBIT, Entry.amount), else_=0)),
                BigInteger,
            ).label("total_debits"),
            func.min(Entry.currency).label("currency"),
        )
        .group_by(Entry.transaction_id)
        .subquery()
    )
    stmt = (
        select(
            Transaction.id,
            Transaction.external_ref,
            Transaction.created_at,
            amount_subq.c.currency,
            amount_subq.c.total_debits,
        )
        .join(amount_subq, amount_subq.c.transaction_id == Transaction.id)
        .where(
            Transaction.status == TransactionStatus.POSTED,
            Transaction.source == TransactionSource.API,
            Transaction.reversal_of.is_(None),
            Transaction.created_at >= window_start,
            Transaction.created_at <= window_end,
            ~exists().where(SettlementLine.matched_transaction_id == Transaction.id),
        )
        .order_by(Transaction.created_at, Transaction.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        _TxnCandidate(id=r[0], external_ref=r[1], created_at=r[2], currency=r[3], amount=r[4])
        for r in rows
    ]


async def _load_line_candidates(
    session: AsyncSession, *, window_start: datetime, window_end: datetime, fuzzy_days: int
) -> list[_LineCandidate]:
    """Widened by `fuzzy_days` on both sides so pass 3 can reach a line
    whose `value_date` lands just outside the true window -- but a line
    pulled in solely by that margin is never itself reported as
    `unexpected_settlement` (see `match()`'s residue step, which re-checks
    the true window)."""
    low = window_start.date() - timedelta(days=fuzzy_days)
    high = window_end.date() + timedelta(days=fuzzy_days)
    stmt = (
        select(
            SettlementLine.id,
            SettlementLine.external_ref,
            SettlementLine.amount,
            SettlementLine.currency,
            SettlementLine.value_date,
        )
        .where(
            SettlementLine.matched_transaction_id.is_(None),
            SettlementLine.value_date >= low,
            SettlementLine.value_date <= high,
        )
        .order_by(SettlementLine.value_date, SettlementLine.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        _LineCandidate(id=r[0], external_ref=r[1], amount=r[2], currency=r[3], value_date=r[4])
        for r in rows
    ]


async def match(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    cutoff_at: datetime,
    fuzzy_days: int,
) -> MatchResult:
    txns = await _load_txn_candidates(session, window_start=window_start, window_end=window_end)
    lines = await _load_line_candidates(
        session, window_start=window_start, window_end=window_end, fuzzy_days=fuzzy_days
    )

    unmatched_txn_ids = {t.id for t in txns}
    unmatched_line_ids = {line.id for line in lines}
    line_matches: dict[uuid.UUID, uuid.UUID] = {}  # settlement_line_id -> transaction_id
    findings: list[ProposedFinding] = []

    # Pass 1: exact (external_ref, amount, currency).
    lines_by_exact_key: dict[tuple[str, int, str], list[_LineCandidate]] = defaultdict(list)
    for line in lines:
        if line.external_ref is not None:
            lines_by_exact_key[(line.external_ref, line.amount, line.currency)].append(line)

    for txn in txns:
        if txn.id not in unmatched_txn_ids or txn.external_ref is None:
            continue
        candidates = [
            line
            for line in lines_by_exact_key.get((txn.external_ref, txn.amount, txn.currency), [])
            if line.id in unmatched_line_ids
        ]
        if not candidates:
            continue
        survivor, *rest = candidates  # already sorted (value_date, id)
        line_matches[survivor.id] = txn.id
        unmatched_line_ids.discard(survivor.id)
        unmatched_txn_ids.discard(txn.id)
        for dup in rest:
            line_matches[dup.id] = txn.id
            unmatched_line_ids.discard(dup.id)
            findings.append(
                ProposedFinding(
                    finding_type=ReconciliationFindingType.DUPLICATE_SETTLEMENT,
                    transaction_id=txn.id,
                    settlement_line_id=dup.id,
                    delta_amount=None,
                    detail={"duplicate_of_line_id": str(survivor.id)},
                )
            )

    # Pass 2: ref-only, any amount.
    lines_by_ref: dict[str, list[_LineCandidate]] = defaultdict(list)
    for line in lines:
        if line.external_ref is not None:
            lines_by_ref[line.external_ref].append(line)

    for txn in txns:
        if txn.id not in unmatched_txn_ids or txn.external_ref is None:
            continue
        candidates = [
            line for line in lines_by_ref.get(txn.external_ref, []) if line.id in unmatched_line_ids
        ]
        if not candidates:
            continue
        # Nearest abs(delta) wins; ties broken by (value_date, id) via the
        # already-sorted iteration order `min()` preserves for equal keys.
        survivor = min(candidates, key=lambda line: abs(line.amount - txn.amount))
        line_matches[survivor.id] = txn.id
        unmatched_line_ids.discard(survivor.id)
        unmatched_txn_ids.discard(txn.id)
        for dup in candidates:
            if dup.id == survivor.id:
                continue
            line_matches[dup.id] = txn.id
            unmatched_line_ids.discard(dup.id)
            findings.append(
                ProposedFinding(
                    finding_type=ReconciliationFindingType.DUPLICATE_SETTLEMENT,
                    transaction_id=txn.id,
                    settlement_line_id=dup.id,
                    delta_amount=None,
                    detail={"duplicate_of_line_id": str(survivor.id)},
                )
            )
        if survivor.currency != txn.currency:
            # Branch on currency BEFORE computing any delta -- a bigint
            # difference across currencies is meaningless.
            findings.append(
                ProposedFinding(
                    finding_type=ReconciliationFindingType.CURRENCY_MISMATCH,
                    transaction_id=txn.id,
                    settlement_line_id=survivor.id,
                    delta_amount=None,
                    detail={
                        "transaction_currency": txn.currency,
                        "settlement_currency": survivor.currency,
                    },
                )
            )
        else:
            delta = survivor.amount - txn.amount
            if delta != 0:
                findings.append(
                    ProposedFinding(
                        finding_type=ReconciliationFindingType.AMOUNT_MISMATCH,
                        transaction_id=txn.id,
                        settlement_line_id=survivor.id,
                        delta_amount=delta,
                        detail={},
                    )
                )

    # Pass 3: fuzzy (amount, currency) + date distance, only when both
    # sides lack an external_ref. Greedy nearest-date; 2+ equidistant is
    # left unmatched, never guessed.
    lines_by_amount_currency: dict[tuple[int, str], list[_LineCandidate]] = defaultdict(list)
    for line in lines:
        if line.external_ref is None:
            lines_by_amount_currency[(line.amount, line.currency)].append(line)

    for txn in txns:
        if txn.id not in unmatched_txn_ids or txn.external_ref is not None:
            continue
        candidates = [
            line
            for line in lines_by_amount_currency.get((txn.amount, txn.currency), [])
            if line.id in unmatched_line_ids
        ]
        txn_date = txn.created_at.date()
        distances = {
            line.id: abs((line.value_date - txn_date).days)
            for line in candidates
            if abs((line.value_date - txn_date).days) <= fuzzy_days
        }
        if not distances:
            continue
        min_dist = min(distances.values())
        nearest = [line for line in candidates if distances.get(line.id) == min_dist]
        if len(nearest) != 1:
            continue  # ambiguous -- never guess
        line = nearest[0]
        line_matches[line.id] = txn.id
        unmatched_txn_ids.discard(txn.id)
        unmatched_line_ids.discard(line.id)

    # Residue, evaluated only against the TRUE window (not the fuzzy-widened
    # candidate set) for lines.
    for txn in txns:
        if txn.id not in unmatched_txn_ids:
            continue
        finding_type = (
            ReconciliationFindingType.IN_FLIGHT
            if txn.created_at > cutoff_at
            else ReconciliationFindingType.MISSING_SETTLEMENT
        )
        findings.append(
            ProposedFinding(
                finding_type=finding_type,
                transaction_id=txn.id,
                settlement_line_id=None,
                delta_amount=None,
                detail={},
            )
        )

    true_low, true_high = window_start.date(), window_end.date()
    for line in lines:
        if line.id not in unmatched_line_ids:
            continue
        if not (true_low <= line.value_date <= true_high):
            continue  # only in the candidate set via fuzzy widening
        findings.append(
            ProposedFinding(
                finding_type=ReconciliationFindingType.UNEXPECTED_SETTLEMENT,
                transaction_id=None,
                settlement_line_id=line.id,
                delta_amount=None,
                detail={},
            )
        )

    for line_id, txn_id in line_matches.items():
        await session.execute(
            update(SettlementLine)
            .where(SettlementLine.id == line_id)
            .values(matched_transaction_id=txn_id)
        )

    return MatchResult(findings=tuple(findings), matched_count=len(line_matches))
