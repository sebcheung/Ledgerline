"""The demo scenario driver behind the dashboard's "Run demo scenario"
button (SPEC.md §12 Phase 6): seeds accounts and transactions, drifts a
settlement feed against them, runs reconciliation, registers two
deliberately failing webhook endpoints, and drains one dispatcher cycle --
so auto-resolution, retry backoff, and the DLQ are all visible on the
dashboard without waiting for `worker/webhook_worker.py` or a manually
triggered reconciliation run.

Lives in `dashboard/`, not `scripts/`: `scripts/` ships in neither the wheel
(`[tool.hatch.build.targets.wheel].packages`) nor the Docker image
(`Dockerfile` never `COPY`s it), so a button handler that `import
scripts.demo` would work from a source checkout and fail in every
environment this app is actually deployed to. `scripts/demo.py` is reduced
to a thin CLI over `run_demo`, the same shape Phase 5 chose for
`worker/webhook_worker.py` around `ledger.webhooks.dispatcher`.

Everything through the reconciliation run happens inside **one** DB
transaction, single commit at the end -- the same shape
`ledger.reconciliation.runner.execute_run` itself uses, and for the same
reason: `pg_try_advisory_xact_lock` only guards concurrent demo runs for as
long as the transaction that acquired it stays open. A session that
commits partway through would release the lock at that first commit
(`AsyncSession` may hand back a different pooled connection on its next
statement), defeating the guard entirely.
"""

import random
import secrets
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.config import get_settings
from ledger.core.posting import EntryRequest, post_transaction
from ledger.models.accounts import Account
from ledger.models.balances import AccountBalance
from ledger.models.enums import AccountType, EntryDirection, TransactionSource
from ledger.models.transactions import Transaction
from ledger.models.webhooks import WebhookEndpoint
from ledger.reconciliation import runner
from ledger.reconciliation.feed import DriftConfig, FeedTransaction, generate_feed
from ledger.reconciliation.ingest import ingest_batch
from ledger.schemas.settlements import SettlementLineIn
from ledger.webhooks.dispatcher import Dispatcher

#: A literal, checked-in crc32 -- never Python's hash(), which is
#: randomized per-process by PYTHONHASHSEED (the same reasoning
#: ledger.reconciliation.runner.RECONCILIATION_RUN_LOCK_KEY documents).
DEMO_LOCK_KEY: int = zlib.crc32(b"ledgerline:demo")

_CURRENCIES = ("USD", "EUR")
_TRANSACTIONS_PER_CURRENCY = 10
#: Comfortably past the default RECON_CUTOFF_LAG_HOURS (24h) so backdated
#: transactions land inside the reconciliation window as real drift
#: candidates, not suppressed `in_flight` findings.
_BACKDATE_DAYS = 3
#: Left un-backdated so `in_flight` suppression (SPEC.md §7) is visible too.
_FRESH_TRANSACTION_COUNT = 2
#: Straddles the default RECON_AUTO_RESOLVE_THRESHOLD_MINOR (500) so a run
#: produces both `auto_resolved` and `unresolved` amount_mismatch findings --
#: what actually makes "recovery is visible" concrete.
_DRIFT = DriftConfig(
    drop_rate=0.10, duplicate_rate=0.10, perturb_rate=0.25, perturb_max_minor=800, extra_lines=3
)


@dataclass(frozen=True, slots=True)
class DemoResult:
    ran: bool
    accounts_created: int
    transactions_posted: int
    batch_id: uuid.UUID | None
    lines_ingested: int
    run_id: uuid.UUID | None
    findings_by_type: dict[str, Any] | None
    endpoints_registered: int
    deliveries_pending: int
    deliveries_dead: int


async def _find_account(
    session: AsyncSession,
    *,
    currency: str,
    name: str | None = None,
    is_suspense: bool = False,
    is_clearing: bool = False,
) -> uuid.UUID | None:
    stmt = select(Account.id).where(Account.currency == currency)
    if name is not None:
        stmt = stmt.where(Account.name == name)
    if is_suspense:
        stmt = stmt.where(Account.is_suspense.is_(True))
    if is_clearing:
        stmt = stmt.where(Account.is_clearing.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _create_account(
    session: AsyncSession,
    *,
    name: str,
    type: AccountType,
    currency: str,
    allow_negative: bool,
    is_suspense: bool = False,
    is_clearing: bool = False,
) -> uuid.UUID:
    row = (
        await session.execute(
            insert(Account)
            .values(
                name=name,
                type=type,
                currency=currency,
                allow_negative=allow_negative,
                is_suspense=is_suspense,
                is_clearing=is_clearing,
            )
            .returning(Account.id)
        )
    ).one()
    await session.execute(
        insert(AccountBalance).values(
            account_id=row.id, currency=currency, balance=0, entry_count=0
        )
    )
    account_id: uuid.UUID = row.id
    return account_id


async def ensure_demo_accounts(
    session: AsyncSession,
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID], int]:
    """Idempotent: looks up each role before creating it, so repeated demo
    runs -- and a demo run alongside `scripts/seed.py` -- never collide
    with `uq_accounts_suspense_per_currency` / `uq_accounts_clearing_per_
    currency` (one per currency, system-wide, not demo-specific -- SPEC.md
    §3 and docs/DECISIONS.md Phase 4)."""
    cash: dict[str, uuid.UUID] = {}
    revenue: dict[str, uuid.UUID] = {}
    created = 0

    for currency in _CURRENCIES:
        cash_id = await _find_account(session, currency=currency, name=f"Demo Cash ({currency})")
        if cash_id is None:
            cash_id = await _create_account(
                session,
                name=f"Demo Cash ({currency})",
                type=AccountType.ASSET,
                currency=currency,
                allow_negative=True,
            )
            created += 1
        cash[currency] = cash_id

        revenue_id = await _find_account(
            session, currency=currency, name=f"Demo Revenue ({currency})"
        )
        if revenue_id is None:
            revenue_id = await _create_account(
                session,
                name=f"Demo Revenue ({currency})",
                type=AccountType.REVENUE,
                currency=currency,
                allow_negative=True,
            )
            created += 1
        revenue[currency] = revenue_id

        # Suspense/clearing: allow_negative=true is an operational
        # requirement for the resolver's auto-resolve paths (docs/
        # DECISIONS.md Phase 4), without which nearly every auto-resolution
        # would strand on InsufficientFunds.
        if await _find_account(session, currency=currency, is_suspense=True) is None:
            await _create_account(
                session,
                name=f"Suspense ({currency})",
                type=AccountType.ASSET,
                currency=currency,
                allow_negative=True,
                is_suspense=True,
            )
            created += 1
        if await _find_account(session, currency=currency, is_clearing=True) is None:
            await _create_account(
                session,
                name=f"Clearing ({currency})",
                type=AccountType.ASSET,
                currency=currency,
                allow_negative=True,
                is_clearing=True,
            )
            created += 1

    return cash, revenue, created


async def _post_and_backdate_transactions(
    session: AsyncSession,
    *,
    cash: dict[str, uuid.UUID],
    revenue: dict[str, uuid.UUID],
    run_tag: str,
    rng: random.Random,
) -> list[FeedTransaction]:
    """Posts `_TRANSACTIONS_PER_CURRENCY` transactions per currency, then
    backdates all but the last `_FRESH_TRANSACTION_COUNT` of them past the
    reconciliation cutoff in one statement. `value_date` for each
    `FeedTransaction` is decided here, up front, to match whichever date the
    backdate UPDATE will actually set -- not read back from `posted.
    created_at`, which at the time of posting still holds the fresh
    (pre-backdate) value."""
    total = len(_CURRENCIES) * _TRANSACTIONS_PER_CURRENCY
    fresh_count = min(_FRESH_TRANSACTION_COUNT, total)
    backdated_date = (datetime.now(UTC) - timedelta(days=_BACKDATE_DAYS)).date()
    fresh_date = datetime.now(UTC).date()

    feed_txns: list[FeedTransaction] = []
    backdate_ids: list[uuid.UUID] = []
    posted_count = 0
    for currency in _CURRENCIES:
        for i in range(_TRANSACTIONS_PER_CURRENCY):
            posted_count += 1
            will_backdate = posted_count <= (total - fresh_count)
            amount = rng.randint(1_000, 50_000)
            posted = await post_transaction(
                session,
                [
                    EntryRequest(cash[currency], EntryDirection.DEBIT, amount, currency),
                    EntryRequest(revenue[currency], EntryDirection.CREDIT, amount, currency),
                ],
                external_ref=f"demo-{run_tag}-{currency}-{i}",
                description="Demo scenario transaction",
                source=TransactionSource.API,
            )
            if will_backdate:
                backdate_ids.append(posted.id)
            feed_txns.append(
                FeedTransaction(
                    external_ref=posted.external_ref,
                    amount=posted.total_debits,
                    currency=posted.currency,
                    value_date=backdated_date if will_backdate else fresh_date,
                )
            )

    if backdate_ids:
        await session.execute(
            update(Transaction)
            .where(Transaction.id.in_(backdate_ids))
            .values(
                created_at=text("now() - make_interval(days => :d)").bindparams(d=_BACKDATE_DAYS)
            )
        )

    return feed_txns


async def _register_demo_endpoints(session: AsyncSession, *, retry_url: str, dead_url: str) -> int:
    for url in (retry_url, dead_url):
        await session.execute(
            insert(WebhookEndpoint).values(url=url, secret=secrets.token_urlsafe(32), active=True)
        )
    return 2


def _empty_result() -> DemoResult:
    return DemoResult(
        ran=False,
        accounts_created=0,
        transactions_posted=0,
        batch_id=None,
        lines_ingested=0,
        run_id=None,
        findings_by_type=None,
        endpoints_registered=0,
        deliveries_pending=0,
        deliveries_dead=0,
    )


async def run_demo(
    session: AsyncSession,
    engine: AsyncEngine,
    *,
    rng_seed: int = 0,
    retry_endpoint_url: str | None = None,
    dead_endpoint_url: str | None = None,
) -> DemoResult:
    """Re-runnable but deliberately **not** idempotent: each call appends a
    fresh scenario under a new `run_tag` rather than trying to be a no-op.
    The button exists to make recovery visible; an idempotent second click
    that changes nothing on screen would read as a broken button."""
    settings = get_settings()
    rng = random.Random(rng_seed)

    acquired = (
        await session.execute(select(text(f"pg_try_advisory_xact_lock({DEMO_LOCK_KEY})")))
    ).scalar_one()
    if not acquired:
        await session.rollback()
        return _empty_result()

    cash, revenue, accounts_created = await ensure_demo_accounts(session)

    run_tag = uuid.uuid4().hex[:8]
    feed_txns = await _post_and_backdate_transactions(
        session, cash=cash, revenue=revenue, run_tag=run_tag, rng=rng
    )

    generated = generate_feed(feed_txns, _DRIFT, rng)
    ingest_result = await ingest_batch(
        session,
        [
            SettlementLineIn(
                external_ref=line.external_ref,
                amount=line.amount,
                currency=line.currency,
                value_date=line.value_date,
                raw=line.raw,
            )
            for line in generated
        ],
    )

    # A fresh random delivery id guarantees a 404 (never a 429/5xx, which
    # the dispatcher would retry instead of deadening) without depending on
    # any real row existing.
    default_dead_url = f"{settings.demo_self_base_url}/v1/webhooks/deliveries/{uuid.uuid4()}/retry"
    endpoints_registered = await _register_demo_endpoints(
        session,
        retry_url=retry_endpoint_url or settings.demo_retry_endpoint_url,
        dead_url=dead_endpoint_url or default_dead_url,
    )

    run_result = await runner.execute_run(session, engine)

    await session.commit()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0), follow_redirects=False
    ) as client:
        cycle = await Dispatcher(engine, client, rng=random.Random(rng_seed)).run_once()

    return DemoResult(
        ran=True,
        accounts_created=accounts_created,
        transactions_posted=len(feed_txns),
        batch_id=ingest_result.batch_id,
        lines_ingested=ingest_result.ingested,
        run_id=run_result.run_id,
        findings_by_type=run_result.findings_by_type,
        endpoints_registered=endpoints_registered,
        deliveries_pending=cycle.retried,
        deliveries_dead=cycle.dead,
    )
