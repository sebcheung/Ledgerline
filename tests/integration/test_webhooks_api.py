"""Integration tests for `/v1/webhooks/*` (SPEC.md §9)."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from ledger.models.enums import WebhookDeliveryStatus
from ledger.models.outbox import OutboxEvent
from ledger.models.webhooks import WebhookDelivery

pytestmark = pytest.mark.integration


async def _create_endpoint(app_client: AsyncClient, **kwargs: object) -> dict[str, Any]:
    payload: dict[str, object] = {"url": "http://127.0.0.1:9/hook"}
    payload.update(kwargs)
    response = await app_client.post("/v1/webhooks/endpoints", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_create_endpoint_returns_a_secret(app_client: AsyncClient) -> None:
    endpoint = await _create_endpoint(app_client)
    assert isinstance(endpoint["secret"], str)
    assert len(endpoint["secret"]) >= 32
    assert endpoint["url"] == "http://127.0.0.1:9/hook"
    assert endpoint["active"] is True


async def test_list_endpoints_never_includes_the_secret(app_client: AsyncClient) -> None:
    await _create_endpoint(app_client)
    response = await app_client.get("/v1/webhooks/endpoints")
    body = response.json()
    assert len(body["items"]) == 1
    # Assert against the raw response keys, not a model that could silently
    # drop an extra field -- the schema has no `secret` attribute at all,
    # but this pins the wire contract directly.
    assert "secret" not in body["items"][0]


async def test_list_endpoints_filters_by_active(app_client: AsyncClient) -> None:
    await _create_endpoint(app_client, active=True)
    await _create_endpoint(app_client, active=False)
    active_only = await app_client.get("/v1/webhooks/endpoints", params={"active": "true"})
    assert len(active_only.json()["items"]) == 1
    inactive_only = await app_client.get("/v1/webhooks/endpoints", params={"active": "false"})
    assert len(inactive_only.json()["items"]) == 1


async def test_list_deliveries_empty(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/webhooks/deliveries")
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "next_cursor": None, "has_more": False}


async def test_retry_unknown_delivery_404s(app_client: AsyncClient) -> None:
    response = await app_client.post(f"/v1/webhooks/deliveries/{uuid.uuid4()}/retry")
    assert response.status_code == 404
    assert response.json()["type"] == "/errors/webhook-delivery-not-found"


async def test_endpoint_list_query_rejects_unknown_fields(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/webhooks/endpoints", params={"bogus": "x"})
    assert response.status_code == 422


async def test_delivery_list_query_rejects_unknown_fields(app_client: AsyncClient) -> None:
    response = await app_client.get("/v1/webhooks/deliveries", params={"bogus": "x"})
    assert response.status_code == 422


async def _insert_delivery(
    session_factory: async_sessionmaker[Any],
    *,
    endpoint_id: uuid.UUID,
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.PENDING,
    created_at: datetime | None = None,
    attempt_count: int = 0,
) -> uuid.UUID:
    """No route creates `webhook_deliveries` rows directly -- only
    `Dispatcher.fan_out()` does -- so these tests insert them straight
    through the ORM, the same way `test_reconciliation_api.py` inserts
    accounts directly to reach states the API alone cannot construct."""
    async with session_factory() as session:
        event_row = (
            await session.execute(
                insert(OutboxEvent)
                .values(event_type="transaction.posted", payload={})
                .returning(OutboxEvent.id)
            )
        ).one()
        values: dict[str, Any] = {
            "event_id": event_row.id,
            "endpoint_id": endpoint_id,
            "status": status,
            "next_attempt_at": datetime.now(UTC),
            "attempt_count": attempt_count,
        }
        if created_at is not None:
            values["created_at"] = created_at
        delivery_row = (
            await session.execute(
                insert(WebhookDelivery).values(**values).returning(WebhookDelivery.id)
            )
        ).one()
        await session.commit()
        return uuid.UUID(str(delivery_row.id))


async def test_list_deliveries_filters_by_status_and_endpoint(
    app_client: AsyncClient, session_factory: async_sessionmaker[Any]
) -> None:
    ep1 = await _create_endpoint(app_client)
    ep2 = await _create_endpoint(app_client)
    await _insert_delivery(
        session_factory, endpoint_id=uuid.UUID(ep1["id"]), status=WebhookDeliveryStatus.PENDING
    )
    dead_id_1 = await _insert_delivery(
        session_factory, endpoint_id=uuid.UUID(ep1["id"]), status=WebhookDeliveryStatus.DEAD
    )
    dead_id_2 = await _insert_delivery(
        session_factory, endpoint_id=uuid.UUID(ep2["id"]), status=WebhookDeliveryStatus.DEAD
    )

    by_status = await app_client.get("/v1/webhooks/deliveries", params={"status": "dead"})
    assert {item["id"] for item in by_status.json()["items"]} == {
        str(dead_id_1),
        str(dead_id_2),
    }

    by_status_and_endpoint = await app_client.get(
        "/v1/webhooks/deliveries", params={"status": "dead", "endpoint_id": ep1["id"]}
    )
    assert [item["id"] for item in by_status_and_endpoint.json()["items"]] == [str(dead_id_1)]


async def test_list_deliveries_stable_order_under_identical_created_at(
    app_client: AsyncClient, session_factory: async_sessionmaker[Any]
) -> None:
    ep = await _create_endpoint(app_client)
    same_ts = datetime.now(UTC)
    ids = [
        await _insert_delivery(session_factory, endpoint_id=uuid.UUID(ep["id"]), created_at=same_ts)
        for _ in range(5)
    ]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await app_client.get("/v1/webhooks/deliveries", params=params)
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]

    assert sorted(seen) == sorted(str(i) for i in ids)
    assert len(seen) == len(set(seen))


async def test_retry_dead_delivery_resets_attempt_count(
    app_client: AsyncClient, session_factory: async_sessionmaker[Any]
) -> None:
    ep = await _create_endpoint(app_client)
    delivery_id = await _insert_delivery(
        session_factory,
        endpoint_id=uuid.UUID(ep["id"]),
        status=WebhookDeliveryStatus.DEAD,
        attempt_count=8,
    )
    response = await app_client.post(f"/v1/webhooks/deliveries/{delivery_id}/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    assert body["claimed_at"] is None


async def test_retry_non_dead_delivery_409s(
    app_client: AsyncClient, session_factory: async_sessionmaker[Any]
) -> None:
    ep = await _create_endpoint(app_client)
    delivery_id = await _insert_delivery(
        session_factory, endpoint_id=uuid.UUID(ep["id"]), status=WebhookDeliveryStatus.PENDING
    )
    response = await app_client.post(f"/v1/webhooks/deliveries/{delivery_id}/retry")
    assert response.status_code == 409
    assert response.json()["type"] == "/errors/delivery-not-retryable"


async def test_concurrent_double_retry_only_one_wins(
    app_client: AsyncClient, session_factory: async_sessionmaker[Any]
) -> None:
    ep = await _create_endpoint(app_client)
    delivery_id = await _insert_delivery(
        session_factory, endpoint_id=uuid.UUID(ep["id"]), status=WebhookDeliveryStatus.DEAD
    )
    responses = await asyncio.gather(
        app_client.post(f"/v1/webhooks/deliveries/{delivery_id}/retry"),
        app_client.post(f"/v1/webhooks/deliveries/{delivery_id}/retry"),
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409]
