"""Integration tests for the Phase 4 reconciliation surface: ingest ->
match -> resolve -> verify, exercised through the HTTP API end to end.

Each finding type is produced deliberately (not just observed as a side
effect), and the two bugs the design review caught -- a resolution-scoped
open-finding index that would double-post, and driving the resolver off
the matcher's full classification instead of the findings INSERT's
`RETURNING` set -- are pinned directly by
`test_rerun_same_window_creates_no_new_findings_and_does_not_double_post`.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.models.accounts import Account

pytestmark = pytest.mark.integration


async def _create_account(app_client: AsyncClient, **kwargs: object) -> dict[str, Any]:
    payload: dict[str, object] = {
        "name": "Account",
        "type": "asset",
        "currency": "USD",
        "allow_negative": False,
    }
    payload.update(kwargs)
    response = await app_client.post("/v1/accounts", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _ledger_pair(app_client: AsyncClient, currency: str = "USD") -> tuple[str, str]:
    cash = await _create_account(app_client, name="Cash", type="asset", currency=currency)
    revenue = await _create_account(app_client, name="Revenue", type="revenue", currency=currency)
    return str(cash["id"]), str(revenue["id"])


async def _suspense_and_clearing(app_client: AsyncClient, currency: str = "USD") -> tuple[str, str]:
    suspense = await _create_account(
        app_client,
        name="Suspense",
        type="asset",
        currency=currency,
        allow_negative=True,
        is_suspense=True,
    )
    clearing = await _create_account(
        app_client,
        name="Clearing",
        type="asset",
        currency=currency,
        allow_negative=True,
        is_clearing=True,
    )
    return str(suspense["id"]), str(clearing["id"])


async def _post_transaction(
    app_client: AsyncClient,
    cash: str,
    revenue: str,
    amount: int,
    *,
    currency: str = "USD",
    **kwargs: object,
) -> dict[str, Any]:
    payload: dict[str, object] = {
        "entries": [
            {"account_id": cash, "direction": "debit", "amount": amount, "currency": currency},
            {"account_id": revenue, "direction": "credit", "amount": amount, "currency": currency},
        ],
    }
    payload.update(kwargs)
    response = await app_client.post("/v1/transactions", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _run(app_client: AsyncClient, key: str) -> dict[str, Any]:
    response = await app_client.post("/v1/reconciliation/runs", headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _findings(app_client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    response = await app_client.get(f"/v1/reconciliation/runs/{run_id}/findings")
    assert response.status_code == 200
    return list(response.json()["items"])


async def _backdate_transaction(engine: AsyncEngine, transaction_id: str, days: int) -> None:
    """Push a transaction's `created_at` before the reconciliation cutoff
    (default 24h lag) so it is classified `missing_settlement`, not
    `in_flight`, without waiting in real time."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE transactions"
                " SET created_at = now() - make_interval(days => :d)"
                " WHERE id = :id"
            ),
            {"d": days, "id": transaction_id},
        )


async def test_clean_feed_produces_zero_findings(app_client: AsyncClient) -> None:
    cash, revenue = await _ledger_pair(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-clean")
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-clean",
                    "amount": 1000,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "clean")
    assert run["findings_by_type"]["observed"] == {}
    assert run["findings_by_type"]["created"] == {}


async def test_missing_settlement_when_feed_drops_a_line(
    app_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-missing")
    await _backdate_transaction(db_engine, str(txn["id"]), days=3)

    run = await _run(app_client, "missing")
    findings = await _findings(app_client, str(run["id"]))
    assert len(findings) == 1
    assert findings[0]["finding_type"] == "missing_settlement"
    assert findings[0]["resolution"] == "unresolved"


async def test_in_flight_suppressed_for_a_recent_transaction(app_client: AsyncClient) -> None:
    cash, revenue = await _ledger_pair(app_client)
    await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-inflight")

    run = await _run(app_client, "inflight")
    findings = await _findings(app_client, str(run["id"]))
    assert len(findings) == 1
    assert findings[0]["finding_type"] == "in_flight"
    assert findings[0]["resolution"] == "suppressed"


async def test_unexpected_settlement_auto_resolves_under_threshold(
    app_client: AsyncClient,
) -> None:
    suspense, clearing = await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 300,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "unexpected")
    findings = await _findings(app_client, str(run["id"]))
    assert len(findings) == 1
    finding = findings[0]
    assert finding["finding_type"] == "unexpected_settlement"
    assert finding["resolution"] == "auto_resolved"
    assert finding["resolving_transaction_id"] is not None

    clearing_bal = (await app_client.get(f"/v1/accounts/{clearing}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert clearing_bal["balance"] == 300
    assert suspense_bal["balance"] == -300


async def test_unexpected_settlement_over_threshold_stays_unresolved(
    app_client: AsyncClient,
) -> None:
    await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 100_000,  # default threshold is 500
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "over-threshold")
    findings = await _findings(app_client, str(run["id"]))
    assert findings[0]["finding_type"] == "unexpected_settlement"
    assert findings[0]["resolution"] == "unresolved"


async def test_unexpected_settlement_negative_amount_auto_resolves_with_reversed_sign(
    app_client: AsyncClient,
) -> None:
    suspense, clearing = await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": -300,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "unexpected-negative")
    findings = await _findings(app_client, str(run["id"]))
    assert findings[0]["resolution"] == "auto_resolved"

    clearing_bal = (await app_client.get(f"/v1/accounts/{clearing}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert clearing_bal["balance"] == -300
    assert suspense_bal["balance"] == 300


async def test_unexpected_settlement_without_clearing_or_suspense_stays_unresolved(
    app_client: AsyncClient,
) -> None:
    """The degraded path: no accounts configured to receive the
    adjustment. Must leave the finding `unresolved` with a warning log,
    never raise."""
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 300,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "no-accounts")
    findings = await _findings(app_client, str(run["id"]))
    assert findings[0]["finding_type"] == "unexpected_settlement"
    assert findings[0]["resolution"] == "unresolved"


async def test_amount_mismatch_with_no_asset_leg_and_no_clearing_stays_unresolved(
    app_client: AsyncClient,
) -> None:
    """A transaction with no asset leg at all (revenue vs. expense) and no
    clearing account to fall back to -- the resolver must give up
    gracefully, not raise."""
    revenue = await _create_account(app_client, name="Revenue", type="revenue")
    expense = await _create_account(app_client, name="Expense", type="expense")
    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {
                    "account_id": str(expense["id"]),
                    "direction": "debit",
                    "amount": 1000,
                    "currency": "USD",
                },
                {
                    "account_id": str(revenue["id"]),
                    "direction": "credit",
                    "amount": 1000,
                    "currency": "USD",
                },
            ],
            "external_ref": "ref-no-asset-leg",
        },
    )
    txn = response.json()
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-no-asset-leg",
                    "amount": 1005,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "no-asset-leg")
    finding = next(
        f
        for f in await _findings(app_client, str(run["id"]))
        if f["finding_type"] == "amount_mismatch"
    )
    assert finding["resolution"] == "unresolved"


async def test_amount_mismatch_without_suspense_account_stays_unresolved(
    app_client: AsyncClient,
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    # is_clearing exists (so the asset-leg fallback would work), but no
    # is_suspense account -- the counter-leg is unavailable either way.
    await _create_account(
        app_client,
        name="Clearing",
        type="asset",
        currency="USD",
        allow_negative=True,
        is_clearing=True,
    )
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-no-suspense")
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-no-suspense",
                    "amount": 1005,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "no-suspense")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "amount_mismatch")
    assert finding["resolution"] == "unresolved"


async def test_adjustment_that_would_go_negative_leaves_finding_unresolved_not_failed(
    app_client: AsyncClient,
) -> None:
    """A degraded path exercised through the real ledger, not a mock: the
    suspense account is created with allow_negative=false (misconfigured,
    against the documented operational requirement), so the adjustment
    raises InsufficientFunds inside the resolver's own nested savepoint.
    The finding must stay unresolved and the run must still complete."""
    suspense = str(
        (
            await _create_account(
                app_client, name="Suspense", type="asset", currency="USD", is_suspense=True
            )
        )["id"]
    )
    await _create_account(
        app_client,
        name="Clearing",
        type="asset",
        currency="USD",
        allow_negative=True,
        is_clearing=True,
    )
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 300,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "insufficient-funds")
    assert run["status"] == "completed"
    findings = await _findings(app_client, str(run["id"]))
    assert findings[0]["resolution"] == "unresolved"

    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert suspense_bal["balance"] == 0  # the failed adjustment left no trace


async def test_manual_resolve_amount_mismatch_post_adjustment(app_client: AsyncClient) -> None:
    cash, revenue = await _ledger_pair(app_client)
    suspense, _clearing = await _suspense_and_clearing(app_client)
    txn = await _post_transaction(
        app_client, cash, revenue, 1000, external_ref="ref-manual-mismatch"
    )
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-manual-mismatch",
                    "amount": 100_000,  # over threshold
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "manual-mismatch")
    finding = next(
        f
        for f in await _findings(app_client, str(run["id"]))
        if f["finding_type"] == "amount_mismatch"
    )
    assert finding["resolution"] == "unresolved"

    response = await app_client.post(
        f"/v1/reconciliation/findings/{finding['id']}/resolve",
        json={"action": "post_adjustment"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution"] == "manually_resolved"

    cash_bal = (await app_client.get(f"/v1/accounts/{cash}")).json()
    assert cash_bal["balance"] == 100_000


async def test_manual_resolve_missing_account_returns_404(app_client: AsyncClient) -> None:
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 100_000,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "manual-no-accounts")
    finding_id = (await _findings(app_client, str(run["id"])))[0]["id"]

    response = await app_client.post(
        f"/v1/reconciliation/findings/{finding_id}/resolve", json={"action": "post_adjustment"}
    )
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/account-not-found"


async def test_amount_mismatch_auto_resolves_and_balances_correctly(
    app_client: AsyncClient,
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    suspense, _clearing = await _suspense_and_clearing(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-mismatch")
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-mismatch",
                    "amount": 1005,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "mismatch")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "amount_mismatch")
    assert finding["delta_amount"] == 5
    assert finding["resolution"] == "auto_resolved"

    cash_bal = (await app_client.get(f"/v1/accounts/{cash}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert cash_bal["balance"] == 1005
    assert suspense_bal["balance"] == -5


async def test_amount_mismatch_negative_delta_auto_resolves(app_client: AsyncClient) -> None:
    cash, revenue = await _ledger_pair(app_client)
    suspense, _clearing = await _suspense_and_clearing(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-neg")
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-neg",
                    "amount": 995,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "neg-mismatch")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "amount_mismatch")
    assert finding["delta_amount"] == -5
    assert finding["resolution"] == "auto_resolved"

    cash_bal = (await app_client.get(f"/v1/accounts/{cash}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert cash_bal["balance"] == 995
    assert suspense_bal["balance"] == 5


async def test_amount_mismatch_asset_to_asset_falls_back_to_clearing(
    app_client: AsyncClient,
) -> None:
    """A transfer between two asset accounts has two asset legs, so the
    single-leg rule can't pick one -- the resolver must fall back to the
    currency's clearing account rather than leaving this unresolved."""
    checking = await _create_account(app_client, name="Checking", type="asset", currency="USD")
    savings = await _create_account(
        app_client, name="Savings", type="asset", currency="USD", allow_negative=True
    )
    suspense, clearing = await _suspense_and_clearing(app_client)

    response = await app_client.post(
        "/v1/transactions",
        json={
            "entries": [
                {
                    "account_id": str(checking["id"]),
                    "direction": "debit",
                    "amount": 1000,
                    "currency": "USD",
                },
                {
                    "account_id": str(savings["id"]),
                    "direction": "credit",
                    "amount": 1000,
                    "currency": "USD",
                },
            ],
            "external_ref": "ref-transfer",
        },
    )
    txn = response.json()
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-transfer",
                    "amount": 1010,
                    "currency": "USD",
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "transfer-mismatch")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "amount_mismatch")
    assert finding["resolution"] == "auto_resolved"

    clearing_bal = (await app_client.get(f"/v1/accounts/{clearing}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert clearing_bal["balance"] == 10
    assert suspense_bal["balance"] == -10


async def test_currency_mismatch_finding_has_null_delta_and_stays_unresolved(
    app_client: AsyncClient,
) -> None:
    cash, revenue = await _ledger_pair(app_client, currency="EUR")
    txn = await _post_transaction(
        app_client, cash, revenue, 1500, currency="EUR", external_ref="ref-ccy"
    )
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": "ref-ccy",
                    "amount": 1500,
                    "currency": "USD",  # wrong currency
                    "value_date": txn["created_at"][:10],
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "ccy-mismatch")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "currency_mismatch")
    assert finding["delta_amount"] is None
    assert finding["resolution"] == "unresolved"


async def test_duplicate_settlement_across_batches_has_no_ledger_effect(
    app_client: AsyncClient,
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-batchdup")
    line = {
        "external_ref": "ref-batchdup",
        "amount": 1000,
        "currency": "USD",
        "value_date": txn["created_at"][:10],
        "raw": {},
    }
    # Two SEPARATE ingest calls -- in-batch dedup does not apply across them.
    await app_client.post("/v1/settlements/ingest", json={"lines": [line]})
    await app_client.post("/v1/settlements/ingest", json={"lines": [line]})

    run = await _run(app_client, "dup")
    findings = await _findings(app_client, str(run["id"]))
    finding = next(f for f in findings if f["finding_type"] == "duplicate_settlement")
    assert finding["resolution"] == "auto_resolved"
    assert finding["resolving_transaction_id"] is None

    cash_bal = (await app_client.get(f"/v1/accounts/{cash}")).json()
    assert cash_bal["balance"] == 1000  # unaffected by the duplicate

    settlements = (await app_client.get("/v1/settlements")).json()["items"]
    assert all(s["matched_transaction_id"] is not None for s in settlements)


async def test_rerun_same_window_creates_no_new_findings_and_does_not_double_post(
    app_client: AsyncClient,
) -> None:
    """Pins the design-review fix directly: an auto-resolved
    unexpected_settlement must not be re-adjusted by a later run over the
    same window (the original bug: a resolution-scoped index would have
    let this double-post)."""
    suspense, clearing = await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 300,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    await _run(app_client, "first-run")
    run2 = await _run(app_client, "second-run")
    assert run2["findings_by_type"]["created"] == {}

    clearing_bal = (await app_client.get(f"/v1/accounts/{clearing}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert clearing_bal["balance"] == 300  # not 600
    assert suspense_bal["balance"] == -300  # not -600


async def test_rerun_preserves_open_missing_settlement(
    app_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-open")
    await _backdate_transaction(db_engine, str(txn["id"]), days=3)

    run1 = await _run(app_client, "open-1")
    findings1 = await _findings(app_client, str(run1["id"]))
    run2 = await _run(app_client, "open-2")
    findings2 = await _findings(app_client, str(run2["id"]))

    assert run2["findings_by_type"]["created"] == {}
    # The open finding still exists (attributed to the first run), not
    # duplicated onto the second.
    assert len(findings1) == 1
    assert len(findings2) == 0
    all_findings = await app_client.get(f"/v1/reconciliation/runs/{run1['id']}/findings")
    assert all_findings.json()["items"][0]["resolution"] == "unresolved"


async def test_advisory_lock_blocks_a_concurrent_run(app_client: AsyncClient) -> None:
    results = await asyncio.gather(
        app_client.post("/v1/reconciliation/runs", headers={"Idempotency-Key": "race-1"}),
        app_client.post("/v1/reconciliation/runs", headers={"Idempotency-Key": "race-2"}),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["type"] == "/errors/reconciliation-run-in-progress"


async def test_manual_resolve_post_adjustment_bypasses_threshold(app_client: AsyncClient) -> None:
    suspense, clearing = await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 100_000,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "manual-bypass")
    finding = (await _findings(app_client, str(run["id"])))[0]
    assert finding["resolution"] == "unresolved"

    response = await app_client.post(
        f"/v1/reconciliation/findings/{finding['id']}/resolve",
        json={"action": "post_adjustment", "note": "ops approved"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution"] == "manually_resolved"
    assert body["resolving_transaction_id"] is not None
    assert body["detail"]["note"] == "ops approved"

    clearing_bal = (await app_client.get(f"/v1/accounts/{clearing}")).json()
    suspense_bal = (await app_client.get(f"/v1/accounts/{suspense}")).json()
    assert clearing_bal["balance"] == 100_000
    assert suspense_bal["balance"] == -100_000


async def test_manual_resolve_suppress_has_no_ledger_effect(app_client: AsyncClient) -> None:
    cash, revenue = await _ledger_pair(app_client)
    await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-suppress")

    run = await _run(app_client, "suppress-run")
    findings = await _findings(app_client, str(run["id"]))
    assert findings[0]["finding_type"] == "in_flight"
    # in_flight is already resolved automatically -- suppress against it
    # must conflict, not silently succeed.
    response = await app_client.post(
        f"/v1/reconciliation/findings/{findings[0]['id']}/resolve",
        json={"action": "suppress"},
    )
    assert response.status_code == 409


async def test_manual_resolve_already_resolved_conflicts(app_client: AsyncClient) -> None:
    suspense, clearing = await _suspense_and_clearing(app_client)
    await app_client.post(
        "/v1/settlements/ingest",
        json={
            "lines": [
                {
                    "external_ref": None,
                    "amount": 100_000,
                    "currency": "USD",
                    "value_date": datetime.now(UTC).date().isoformat(),
                    "raw": {},
                }
            ]
        },
    )
    run = await _run(app_client, "double-resolve")
    finding_id = (await _findings(app_client, str(run["id"])))[0]["id"]

    first = await app_client.post(
        f"/v1/reconciliation/findings/{finding_id}/resolve", json={"action": "suppress"}
    )
    assert first.status_code == 200

    second = await app_client.post(
        f"/v1/reconciliation/findings/{finding_id}/resolve", json={"action": "suppress"}
    )
    assert second.status_code == 409
    assert second.json()["type"] == "/errors/finding-already-resolved"


async def test_manual_resolve_post_adjustment_invalid_for_missing_settlement(
    app_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    cash, revenue = await _ledger_pair(app_client)
    txn = await _post_transaction(app_client, cash, revenue, 1000, external_ref="ref-invalid")
    await _backdate_transaction(db_engine, str(txn["id"]), days=3)

    run = await _run(app_client, "invalid-action")
    finding_id = (await _findings(app_client, str(run["id"])))[0]["id"]

    response = await app_client.post(
        f"/v1/reconciliation/findings/{finding_id}/resolve", json={"action": "post_adjustment"}
    )
    assert response.status_code == 422
    assert response.json()["type"] == "/errors/invalid-finding-resolution"


async def test_clearing_account_per_currency_conflict(app_client: AsyncClient) -> None:
    await _create_account(
        app_client, name="Clearing1", type="asset", currency="USD", is_clearing=True
    )
    response = await app_client.post(
        "/v1/accounts",
        json={
            "name": "Clearing2",
            "type": "asset",
            "currency": "USD",
            "allow_negative": False,
            "is_clearing": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["type"] == "/errors/clearing-account-exists"


async def test_clearing_account_must_be_asset_type(db_session: AsyncSession) -> None:
    """`ck_accounts_clearing_is_asset` is a DB-level backstop, not something
    the API translates into a typed error -- pinned directly against the
    constraint rather than through HTTP (a non-`LedgerError` 500 does not
    round-trip cleanly through `RequestIdMiddleware`'s `BaseHTTPMiddleware`,
    a pre-existing framework quirk unrelated to Phase 4)."""
    with pytest.raises(IntegrityError, match="ck_accounts_clearing_is_asset"):
        await db_session.execute(
            insert(Account).values(
                name="BadClearing",
                type="liability",
                currency="USD",
                allow_negative=False,
                is_clearing=True,
            )
        )


async def test_get_run_not_found(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/reconciliation/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/reconciliation-run-not-found"


async def test_resolve_finding_not_found(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/v1/reconciliation/findings/00000000-0000-0000-0000-000000000000/resolve",
        json={"action": "suppress"},
    )
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/reconciliation-finding-not-found"
