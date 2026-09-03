"""Locust load test (SPEC.md §12 Phase 7): throughput and p99 posting
latency against a running Ledgerline instance.

Not part of the `ledger`/`worker`/`dashboard` wheel or the Docker image --
run manually against docker-compose or a Fly deployment, never in CI (see
`loadtest/README.md` for why). Requires the `loadtest` extra:
`pip install -e ".[loadtest]"`.

Usage:
    LEDGERLINE_API_KEY=<key> locust -f loadtest/locustfile.py --host http://localhost:8000
"""

import os
import random
import uuid
from typing import Any

from locust import HttpUser, between, task

_API_KEY = os.environ.get("LEDGERLINE_API_KEY", "")


class LedgerlineUser(HttpUser):
    wait_time = between(0.05, 0.25)

    def on_start(self) -> None:
        self.client.headers["Authorization"] = f"Bearer {_API_KEY}"
        self.cash_id = self._create_account("Load Test Cash", "asset")
        self.revenue_id = self._create_account("Load Test Revenue", "revenue")
        self.shared_cash_id, self.shared_revenue_id = _shared_accounts(self.client)

    def _create_account(self, name: str, account_type: str) -> str:
        response = self.client.post(
            "/v1/accounts",
            json={
                "name": f"{name} {uuid.uuid4()}",
                "type": account_type,
                "currency": "USD",
                "allow_negative": account_type == "asset",
            },
            name="POST /v1/accounts",
        )
        result: str = response.json()["id"]
        return result

    @task(10)
    def post_transaction(self) -> None:
        amount = random.randint(100, 10_000)
        self.client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {
                        "account_id": self.cash_id,
                        "direction": "debit",
                        "amount": amount,
                        "currency": "USD",
                    },
                    {
                        "account_id": self.revenue_id,
                        "direction": "credit",
                        "amount": amount,
                        "currency": "USD",
                    },
                ]
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
            name="POST /v1/transactions",
        )

    @task(1)
    def post_against_shared_contended_account(self) -> None:
        """Deliberately contended: every user posts against the *same*
        pair of accounts here, so this task -- unlike `post_transaction`
        above -- actually exercises `account_balances` row-lock contention
        under concurrency, the property `ledger.core.posting`'s ordered
        `FOR UPDATE` locking exists to make safe."""
        amount = random.randint(100, 5_000)
        self.client.post(
            "/v1/transactions",
            json={
                "entries": [
                    {
                        "account_id": self.shared_cash_id,
                        "direction": "debit",
                        "amount": amount,
                        "currency": "USD",
                    },
                    {
                        "account_id": self.shared_revenue_id,
                        "direction": "credit",
                        "amount": amount,
                        "currency": "USD",
                    },
                ]
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
            name="POST /v1/transactions (shared, contended)",
        )

    @task(1)
    def replay_an_idempotent_request(self) -> None:
        """Measures the replay path specifically (`name=` keeps it out of
        the plain POST /v1/transactions timing bucket)."""
        amount = random.randint(100, 10_000)
        key = str(uuid.uuid4())
        body = {
            "entries": [
                {
                    "account_id": self.cash_id,
                    "direction": "debit",
                    "amount": amount,
                    "currency": "USD",
                },
                {
                    "account_id": self.revenue_id,
                    "direction": "credit",
                    "amount": amount,
                    "currency": "USD",
                },
            ]
        }
        self.client.post(
            "/v1/transactions",
            json=body,
            headers={"Idempotency-Key": key},
            name="POST /v1/transactions (replay, first)",
        )
        self.client.post(
            "/v1/transactions",
            json=body,
            headers={"Idempotency-Key": key},
            name="POST /v1/transactions (replay, second)",
        )

    @task(3)
    def get_account(self) -> None:
        self.client.get(f"/v1/accounts/{self.cash_id}", name="GET /v1/accounts/:id")

    @task(2)
    def list_transactions(self) -> None:
        self.client.get("/v1/transactions", name="GET /v1/transactions")


_shared_ids: tuple[str, str] | None = None


def _shared_accounts(client: Any) -> tuple[str, str]:
    """Lazily create the shared contended account pair exactly once per
    Locust worker process. Not thread-safe against a genuine race between
    two users starting in the same instant -- acceptable here, since a
    handful of extra shared-account pairs would only *reduce* the
    contention the shared-account task is trying to create, not break
    correctness."""
    global _shared_ids
    if _shared_ids is None:
        cash = client.post(
            "/v1/accounts",
            json={
                "name": f"Shared Cash {uuid.uuid4()}",
                "type": "asset",
                "currency": "USD",
                "allow_negative": True,
            },
            name="POST /v1/accounts",
        ).json()["id"]
        revenue = client.post(
            "/v1/accounts",
            json={
                "name": f"Shared Revenue {uuid.uuid4()}",
                "type": "revenue",
                "currency": "USD",
            },
            name="POST /v1/accounts",
        ).json()["id"]
        _shared_ids = (cash, revenue)
    return _shared_ids
