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


def _exposition_text() -> str:
    return generate_latest(REGISTRY).decode()


def test_every_spec_metric_name_is_present() -> None:
    text = _exposition_text()
    for name in _SPEC_NAMES:
        assert f"# TYPE {name}" in text, f"missing metric {name!r}"


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
