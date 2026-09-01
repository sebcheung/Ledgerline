from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _line(**kwargs: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_ref": "ref-1",
        "amount": 1000,
        "currency": "USD",
        "value_date": datetime.now(UTC).date().isoformat(),
        "raw": {"note": "test"},
    }
    payload.update(kwargs)
    return payload


async def test_ingest_returns_batch_id_and_counts(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/settlements/ingest", json={"lines": [_line()]})
    assert response.status_code == 201
    body = response.json()
    assert body["ingested"] == 1
    assert body["deduplicated"] == 0
    assert "batch_id" in body


async def test_ingest_dedups_identical_lines_within_one_batch(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/settlements/ingest", json={"lines": [_line(), _line()]})
    body = response.json()
    assert body["ingested"] == 1
    assert body["deduplicated"] == 1


async def test_ingest_does_not_dedup_across_currency(app_client: AsyncClient) -> None:
    """Documented Phase 4 deviation: the dedup key adds currency to
    SPEC.md §7's literal (external_ref, amount, value_date) tuple."""
    response = await app_client.post(
        "/v1/settlements/ingest",
        json={"lines": [_line(currency="USD"), _line(currency="EUR")]},
    )
    body = response.json()
    assert body["ingested"] == 2
    assert body["deduplicated"] == 0


async def test_ingest_does_not_dedup_null_external_ref_lines(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/v1/settlements/ingest",
        json={"lines": [_line(external_ref=None), _line(external_ref=None)]},
    )
    body = response.json()
    assert body["ingested"] == 2
    assert body["deduplicated"] == 0


async def test_ingest_preserves_raw_payload_verbatim(app_client: AsyncClient) -> None:
    raw = {"provider": "acme", "nested": {"a": 1}}
    await app_client.post("/v1/settlements/ingest", json={"lines": [_line(raw=raw)]})
    listed = await app_client.get("/v1/settlements")
    assert listed.json()["items"][0]["raw"] == raw


async def test_list_settlements_filters_by_batch_id(app_client: AsyncClient) -> None:
    first = (
        await app_client.post("/v1/settlements/ingest", json={"lines": [_line(external_ref="a")]})
    ).json()
    await app_client.post("/v1/settlements/ingest", json={"lines": [_line(external_ref="b")]})

    listed = await app_client.get("/v1/settlements", params={"batch_id": first["batch_id"]})
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["external_ref"] == "a"


async def test_list_settlements_filters_by_matched(app_client: AsyncClient) -> None:
    await app_client.post("/v1/settlements/ingest", json={"lines": [_line()]})

    unmatched = await app_client.get("/v1/settlements", params={"matched": "false"})
    assert len(unmatched.json()["items"]) == 1

    matched = await app_client.get("/v1/settlements", params={"matched": "true"})
    assert len(matched.json()["items"]) == 0


async def test_list_settlements_paginates(app_client: AsyncClient) -> None:
    for i in range(5):
        await app_client.post(
            "/v1/settlements/ingest", json={"lines": [_line(external_ref=f"ref-{i}")]}
        )

    page1 = await app_client.get("/v1/settlements", params={"limit": 3})
    body1 = page1.json()
    assert len(body1["items"]) == 3
    assert body1["has_more"] is True

    page2 = await app_client.get(
        "/v1/settlements", params={"limit": 3, "cursor": body1["next_cursor"]}
    )
    body2 = page2.json()
    assert len(body2["items"]) == 2
    assert body2["has_more"] is False

    seen_ids = {item["id"] for item in body1["items"]} | {item["id"] for item in body2["items"]}
    assert len(seen_ids) == 5
