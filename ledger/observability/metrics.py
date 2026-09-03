"""The Prometheus metric registry (SPEC.md §9 Phase 7).

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
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.enums import ReconciliationFindingType, WebhookDeliveryStatus
from ledger.models.reconciliation import ReconciliationFinding
from ledger.models.webhooks import WebhookDelivery

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


async def refresh_db_gauges(session: AsyncSession) -> None:
    """Called once per `GET /metrics` scrape, before `generate_latest`.
    Not a `prometheus_client.registry.Collector` -- those are synchronous,
    and this needs an `await`."""
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
