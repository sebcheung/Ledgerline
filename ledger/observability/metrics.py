"""The Prometheus metric registry (SPEC.md §9 Phase 7, extended in Phase 8
slice 1 -- see docs/DECISIONS.md).

A dedicated `CollectorRegistry`, not `prometheus_client`'s global default:
keeps the default `process_*`/`python_gc_*` collectors out of `/metrics`
output, and makes double-registration impossible across the many
`create_app()` calls the test suite makes in one process (registering
against the default registry a second time raises).

Framework-free -- imported by `ledger.core.posting`, which must never
import `fastapi` (see `ledger.core.errors`'s module docstring). Split by
one rule: an event that only ever happens inside the API process is a
Counter, incremented inline at the call site that already logs it; a
quantity whose ground truth is a database table (because the process that
produces it, e.g. `worker.webhook_worker`, has no HTTP server to scrape)
is a Gauge refreshed from that table at scrape time by `refresh_db_gauges`,
called from `GET /metrics` (`ledger/api/routes/metrics.py`) just before
`generate_latest`.

`refresh_db_gauges` swallows its own DB errors (Phase 8 slice 1): the
gauges it maintains are the only signal an operator has that something is
wrong, so a DB outage must not also take `/metrics` itself down with a
500. On failure it logs, bumps `metrics_db_refresh_failures_total`, and
leaves every Gauge at its last-known value -- `prometheus_client` Gauges
already retain the last `.set()` value when `.set()` is not called again,
so "do nothing" is the correct degraded behavior here, not a fallback
query or a cached default.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.enums import ReconciliationFindingType, WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.reconciliation import ReconciliationFinding
from ledger.models.webhooks import WebhookDelivery
from ledger.observability.logging import get_logger

logger = get_logger(__name__)

REGISTRY = CollectorRegistry()

TRANSACTIONS_POSTED = Counter(
    "transactions_posted_total", "Ledger transactions successfully posted", registry=REGISTRY
)
ENTRIES_WRITTEN = Counter("entries_written_total", "Ledger entries written", registry=REGISTRY)
IDEMPOTENCY_REPLAYS = Counter(
    "idempotency_replays_total",
    "Requests served from a stored idempotent response",
    registry=REGISTRY,
)
IDEMPOTENCY_CONFLICTS = Counter(
    "idempotency_conflicts_total",
    "Requests that hit an in-flight idempotency key lock",
    registry=REGISTRY,
)
POSTING_LATENCY = Histogram(
    "posting_latency_seconds",
    "Time spent inside ledger.core.posting.post_transaction",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)

WEBHOOK_DELIVERIES = Gauge(
    "webhook_deliveries_total",
    "Webhook deliveries by status (scraped from the database, not an "
    "in-process counter -- the dispatcher runs in a separate process)",
    labelnames=("status",),
    registry=REGISTRY,
)
WEBHOOK_DLQ_DEPTH = Gauge(
    "webhook_dlq_depth", "Webhook deliveries currently dead-lettered", registry=REGISTRY
)
RECONCILIATION_FINDINGS = Gauge(
    "reconciliation_findings_total",
    "Reconciliation findings by type (scraped from the database)",
    labelnames=("type",),
    registry=REGISTRY,
)
OUTBOX_LAG = Gauge(
    "outbox_lag_seconds",
    "Age of the oldest outbox event not yet fanned out (0 when none are "
    "waiting) -- scraped from the database, since only Dispatcher.fan_out "
    "advances fanned_out_at and it has no HTTP server of its own to scrape",
    registry=REGISTRY,
)

#: Phase 8 slice 1 (docs/DECISIONS.md): webhook delivery events that only
#: ever happen inside `ledger.webhooks.dispatcher.Dispatcher`, which -- like
#: `worker.webhook_worker` -- has no HTTP server to scrape, so unlike
#: `TRANSACTIONS_POSTED` et al. these are Counters/Histograms incremented at
#: the dispatcher's call sites rather than Gauges refreshed here.
WEBHOOK_STALE_CLAIMS_SWEPT = Counter(
    "webhook_stale_claims_swept_total",
    "Webhook deliveries reclaimed from a stuck delivering state back to "
    "pending by Dispatcher.sweep_stale_claims",
    registry=REGISTRY,
)
WEBHOOK_DELIVERY_ATTEMPTS = Counter(
    "webhook_delivery_attempts_total",
    "Webhook delivery attempts recorded by Dispatcher._record, by outcome",
    labelnames=("outcome",),
    registry=REGISTRY,
)
WEBHOOK_DELIVERY_LATENCY = Histogram(
    "webhook_delivery_latency_seconds",
    "Time from an outbox event's creation to its successful webhook "
    "delivery -- a queue that can legitimately take minutes under backoff, "
    "hence buckets reaching to 15 minutes rather than posting_latency_"
    "seconds' sub-5-second range",
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 300, 900),
    registry=REGISTRY,
)

#: Not yet incremented anywhere (Phase 8 slice 1 only defines and registers
#: it); a later slice's dispatcher/engine error-handling work wires this up.
DB_ERRORS = Counter(
    "db_errors_total",
    "Database errors encountered outside a request's own error handling",
    labelnames=("operation",),
    registry=REGISTRY,
)
METRICS_DB_REFRESH_FAILURES = Counter(
    "metrics_db_refresh_failures_total",
    "Failures raised by refresh_db_gauges itself while scraping GET /metrics "
    "-- see this module's docstring for why these are swallowed, not raised",
    registry=REGISTRY,
)


async def refresh_db_gauges(session: AsyncSession) -> None:
    """Called once per `GET /metrics` scrape, before `generate_latest`.
    Not a `prometheus_client.registry.Collector` -- those are synchronous,
    and this needs an `await`.

    Wrapped in a blanket `try/except`: a DB outage must degrade `/metrics`
    to stale-but-present gauges, not a 500 on the one endpoint an operator
    needs most during an outage (Phase 8 slice 1). See the module docstring
    for why "just don't call .set()" is sufficient degraded behavior.
    """
    try:
        delivery_rows = await session.execute(
            select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status)
        )
        counts = dict.fromkeys(WebhookDeliveryStatus, 0)
        for status, n in delivery_rows:
            counts[status] = n
        for status, n in counts.items():
            WEBHOOK_DELIVERIES.labels(status=status.value).set(n)
        WEBHOOK_DLQ_DEPTH.set(counts[WebhookDeliveryStatus.DEAD])

        finding_rows = await session.execute(
            select(ReconciliationFinding.finding_type, func.count()).group_by(
                ReconciliationFinding.finding_type
            )
        )
        finding_counts = dict.fromkeys(ReconciliationFindingType, 0)
        for finding_type, n in finding_rows:
            finding_counts[finding_type] = n
        for finding_type, n in finding_counts.items():
            RECONCILIATION_FINDINGS.labels(type=finding_type.value).set(n)

        lag_seconds = await session.scalar(
            select(
                func.coalesce(
                    func.extract("epoch", func.now() - func.min(OutboxEvent.created_at)),
                    0,
                )
            ).where(OutboxEvent.fanned_out_at.is_(None))
        )
        OUTBOX_LAG.set(float(lag_seconds) if lag_seconds is not None else 0.0)
    except Exception:
        logger.error("metrics_db_refresh_failed", exc_info=True)
        METRICS_DB_REFRESH_FAILURES.inc()
