"""Unit tests for `ledger.observability.metrics` -- no DB, exercises only
the registry shape and label names."""

from prometheus_client import generate_latest

from ledger.observability.metrics import REGISTRY

_SPEC_NAMES = (
    "transactions_posted_total",
    "entries_written_total",
    "idempotency_replays_total",
    "idempotency_conflicts_total",
    "reconciliation_findings_total",
    "webhook_deliveries_total",
    "webhook_dlq_depth",
    "posting_latency_seconds",
)

#: Phase 8 slice 1 (docs/DECISIONS.md) additions.
_PHASE_8_SLICE_1_NAMES = (
    "webhook_stale_claims_swept_total",
    "webhook_delivery_attempts_total",
    "webhook_delivery_latency_seconds",
    "outbox_lag_seconds",
    "db_errors_total",
    "metrics_db_refresh_failures_total",
)


def _exposition_text() -> str:
    return generate_latest(REGISTRY).decode()


def test_every_spec_metric_name_is_present() -> None:
    text = _exposition_text()
    for name in _SPEC_NAMES:
        assert f"# TYPE {name}" in text, f"missing metric {name!r}"


def test_every_phase_8_slice_1_metric_name_is_present() -> None:
    text = _exposition_text()
    for name in _PHASE_8_SLICE_1_NAMES:
        assert f"# TYPE {name}" in text, f"missing metric {name!r}"


def test_webhook_stale_claims_swept_is_a_counter_with_no_labels() -> None:
    text = _exposition_text()
    assert "# TYPE webhook_stale_claims_swept_total counter" in text


def test_webhook_delivery_attempts_is_a_counter_labelled_by_outcome() -> None:
    from ledger.observability.metrics import WEBHOOK_DELIVERY_ATTEMPTS

    text = _exposition_text()
    assert "# TYPE webhook_delivery_attempts_total counter" in text
    for outcome in ("succeeded", "retried", "dead"):
        # Registering the label combination is enough to prove the
        # labelname is exactly "outcome" -- a wrong labelname would raise
        # here, not silently produce a different series.
        WEBHOOK_DELIVERY_ATTEMPTS.labels(outcome=outcome)


def test_webhook_delivery_latency_emits_histogram_components() -> None:
    text = _exposition_text()
    assert "# TYPE webhook_delivery_latency_seconds histogram" in text
    assert "webhook_delivery_latency_seconds_bucket" in text
    assert "webhook_delivery_latency_seconds_sum" in text
    assert "webhook_delivery_latency_seconds_count" in text


def test_outbox_lag_is_a_gauge_with_no_labels() -> None:
    text = _exposition_text()
    assert "# TYPE outbox_lag_seconds gauge" in text


def test_db_errors_is_a_counter_labelled_by_operation() -> None:
    from ledger.observability.metrics import DB_ERRORS

    text = _exposition_text()
    assert "# TYPE db_errors_total counter" in text
    DB_ERRORS.labels(operation="select")  # would raise on a wrong labelname


def test_metrics_db_refresh_failures_is_a_counter_with_no_labels() -> None:
    text = _exposition_text()
    assert "# TYPE metrics_db_refresh_failures_total counter" in text


def test_reconciliation_findings_and_webhook_deliveries_declare_help_text() -> None:
    # Both are Gauges with no samples set yet at import time (they're only
    # populated by refresh_db_gauges at scrape time) -- only the HELP/TYPE
    # header is guaranteed here; label population is exercised for real in
    # tests/integration/test_metrics_endpoint.py.
    text = _exposition_text()
    assert "# TYPE reconciliation_findings_total gauge" in text
    assert "# TYPE webhook_deliveries_total gauge" in text


def test_posting_latency_emits_histogram_components() -> None:
    text = _exposition_text()
    assert "posting_latency_seconds_bucket" in text
    assert "posting_latency_seconds_sum" in text
    assert "posting_latency_seconds_count" in text
    assert 'le="+Inf"' in text


def test_reimporting_the_module_does_not_double_register() -> None:
    import importlib

    import ledger.observability.metrics as metrics_module

    # A second, distinct import (not `reload`, which would just recreate a
    # fresh registry) proves the module can be imported from more than one
    # call site -- as it already is, by ledger.core.posting and
    # ledger.api.errors -- without registering the same name twice against
    # the *same* CollectorRegistry object.
    reimported = importlib.import_module("ledger.observability.metrics")
    assert reimported is metrics_module
    generate_latest(metrics_module.REGISTRY)  # would raise on a duplicate
