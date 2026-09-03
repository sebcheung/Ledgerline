"""CLI wrapper over `dashboard.demo.run_demo` (SPEC.md §12 Phase 6): seeds a
scenario with settlement drift and webhook failures so reconciliation
auto-resolution and retry/DLQ recovery are visible on the dashboard.

All the logic lives in `dashboard/demo.py`, not here -- `scripts/` ships in
neither the wheel nor the Docker image (see that module's docstring), so
this file is deliberately thin, the same shape `worker/webhook_worker.py`
takes around `ledger.webhooks.dispatcher`.
"""

import argparse
import asyncio

from dashboard.demo import run_demo
from ledger.db.engine import engine
from ledger.db.session import async_session_factory


async def _main(
    *, seed: int, retry_endpoint_url: str | None, dead_endpoint_url: str | None
) -> None:
    async with async_session_factory() as session:
        result = await run_demo(
            session,
            engine,
            rng_seed=seed,
            retry_endpoint_url=retry_endpoint_url,
            dead_endpoint_url=dead_endpoint_url,
        )
    await engine.dispose()

    if not result.ran:
        print("another demo run is already in progress -- try again shortly")
        raise SystemExit(1)

    print(
        f"accounts created: {result.accounts_created}\n"
        f"transactions posted: {result.transactions_posted}\n"
        f"settlement lines ingested: {result.lines_ingested} (batch {result.batch_id})\n"
        f"reconciliation run: {result.run_id}\n"
        f"findings by type: {result.findings_by_type}\n"
        f"webhook endpoints registered: {result.endpoints_registered}\n"
        f"deliveries pending retry: {result.deliveries_pending}\n"
        f"deliveries dead: {result.deliveries_dead}"
    )


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--retry-endpoint-url", default=None)
    parser.add_argument("--dead-endpoint-url", default=None)
    args = parser.parse_args()
    asyncio.run(
        _main(
            seed=args.seed,
            retry_endpoint_url=args.retry_endpoint_url,
            dead_endpoint_url=args.dead_endpoint_url,
        )
    )


if __name__ == "__main__":
    _cli()
