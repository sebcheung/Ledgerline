"""Entry point for the webhook delivery worker process (SPEC.md §8).

Deliberately thin: all real dispatch logic lives in
`ledger.webhooks.dispatcher.Dispatcher`, which is covered by the `ledger`
coverage gate (`pyproject.toml`'s `[tool.coverage.run].source`); `worker/`
is not. This module only wires the process lifecycle -- config, the HTTP
client, signal handling -- around that class.
"""

import asyncio
import contextlib
import logging
import signal

import httpx

from ledger.config import get_settings
from ledger.db.engine import engine
from ledger.observability.logging import configure_logging
from ledger.webhooks.dispatcher import Dispatcher

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows has no add_signal_handler for these signals -- local dev
        # relies on Ctrl+C raising KeyboardInterrupt instead, which
        # asyncio.run below still surfaces cleanly. CI and production both
        # run on Linux, where this suppress is never triggered.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    timeout = httpx.Timeout(
        connect=settings.webhook_connect_timeout_seconds,
        read=settings.webhook_read_timeout_seconds,
        write=settings.webhook_read_timeout_seconds,
        pool=settings.webhook_read_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        dispatcher = Dispatcher(engine, client, settings=settings)
        logger.info("webhook_worker.started")
        await dispatcher.run_forever(stop)
    logger.info("webhook_worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
