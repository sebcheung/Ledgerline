"""Integration tests for `GET /metrics` (SPEC.md §9 Phase 7).

Delta assertions only: `ledger.observability.metrics.REGISTRY` is a
process-wide, module-level registry, so counters accumulate across every
test that runs in this pytest process -- scrape, act, scrape, and compare
the difference, never an absolute value.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.enums import (
    ReconciliationFindingType,
    ReconciliationRunStatus,
    WebhookDeliveryStatus,
)
from ledger.models.outbox import OutboxEvent
from ledger.models.reconciliation import ReconciliationFinding, ReconciliationRun
from ledger.models.webhooks import WebhookDelivery, WebhookEndpoint

pytestmark = pytest.mark.integration


def _samples(text: str, sample_name: str) -> dict[tuple[tuple[str, str], ...], float]:
    """Match on the exposed *sample* name (e.g. `..._total`), not the family
    name -- `text_string_to_metric_families` strips the `_total`/`_count`
    suffix from `family.name` for counters and histograms, so matching on
    `family.name` would silently never find anything."""
    return {
        tuple(sorted(s.labels.items())): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
        if s.name == sample_name
    }


def _scalar(text: str, sample_name: str) -> float:
    samples = _samples(text, sample_name)
    return next(iter(samples.values()), 0.0)


async def _create_account(app_client: AsyncClient, **kwargs: object) -> str:
    payload = {"name": "Account", "type": "asset", "currency": "USD", "allow_negative": False}
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_metrics_content_type(app_client: AsyncClient) -> None:
    response = await app_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_posting_a_transaction_moves_the_transaction_and_entry_counters(
    app_client: AsyncClient,
) -> None:
    before = (await app_client.get("/metrics")).text
    posted_before = _scalar(before, "transactions_posted_total")
    entries_before = _scalar(before, "entries_written_total")
    count_before = _scalar(before, "posting_latency_seconds_count")

    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {"account_id": cash, "direction": "debit", "amount": 500, "currency": "USD"},
                {"account_id": revenue, "direction": "credit", "amount": 500, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 201

    after = (await app_client.get("/metrics")).text
    assert _scalar(after, "transactions_posted_total") == posted_before + 1
    assert _scalar(after, "entries_written_total") == entries_before + 2
    assert _scalar(after, "posting_latency_seconds_count") == count_before + 1


async def test_replayed_request_moves_the_replay_counter(app_client: AsyncClient) -> None:
    cash = await _create_account(app_client, name="Cash", type="asset")
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    body = {
        "entries": [
            {"account_id": cash, "direction": "debit", "amount": 200, "currency": "USD"},
            {"account_id": revenue, "direction": "credit", "amount": 200, "currency": "USD"},
        ]
    }
    key = str(uuid.uuid4())
    first = await app_client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201

    before = (await app_client.get("/metrics")).text
    replays_before = _scalar(before, "idempotency_replays_total")

    replay = await app_client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replay") == "true"

    after = (await app_client.get("/metrics")).text
    assert _scalar(after, "idempotency_replays_total") == replays_before + 1


async def test_dead_delivery_is_reflected_in_the_dlq_gauge(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    endpoint_row = (
        await db_session.execute(
            insert(WebhookEndpoint)
            .values(url="http://example.invalid/hook", secret="s", active=True)
            .returning(WebhookEndpoint.id)
        )
    ).one()
    event_row = (
        await db_session.execute(
            insert(OutboxEvent)
            .values(event_type="transaction.posted", payload={"a": 1})
            .returning(OutboxEvent.id)
        )
    ).one()
    await db_session.execute(
        insert(WebhookDelivery).values(
            event_id=event_row.id,
            endpoint_id=endpoint_row.id,
            status=WebhookDeliveryStatus.DEAD,
            attempt_count=8,
            next_attempt_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.commit()

    response = await app_client.get("/metrics")
    text = response.text
    dlq_depth = _scalar(text, "webhook_dlq_depth")
    dead_samples = _samples(text, "webhook_deliveries_total")

    assert dlq_depth >= 1
    assert dead_samples[(("status", WebhookDeliveryStatus.DEAD.value),)] >= 1


async def test_reconciliation_findings_gauge_reflects_findings_by_type(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    run_row = (
        await db_session.execute(
            insert(ReconciliationRun)
            .values(
                window_start=now,
                window_end=now,
                cutoff_at=now,
                status=ReconciliationRunStatus.COMPLETED,
            )
            .returning(ReconciliationRun.id)
        )
    ).one()
    await db_session.execute(
        insert(ReconciliationFinding).values(
            run_id=run_row.id,
            finding_type=ReconciliationFindingType.MISSING_SETTLEMENT,
        )
    )
    await db_session.commit()

    response = await app_client.get("/metrics")
    samples = _samples(response.text, "reconciliation_findings_total")
    assert samples[(("type", ReconciliationFindingType.MISSING_SETTLEMENT.value),)] >= 1
