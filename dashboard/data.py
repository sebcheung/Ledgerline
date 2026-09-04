"""Per-panel dashboard context loaders.

Shared between the full page route, the fragment routes, and the SSE
snapshot generator (`dashboard/sse.py`) -- kept in its own module, separate
from `dashboard/views.py`, specifically so `sse.py` never has to import
`views.py` (which imports the SSE route) and back again.

`PANEL_NAMES`/`PANEL_LOADERS` are the single place the four panel names are
enumerated; `tests/unit/test_dashboard_templates.py` walks this list to
assert every name has both a `partials/_<name>.html` template and a matching
`sse-swap="<name>"` in `index.html`, so the two can never silently drift
apart.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.readmodels.balances import load_balances
from ledger.readmodels.reconciliation import (
    load_findings_for_run,
    load_findings_summary,
    load_run_history,
)
from ledger.readmodels.transactions import load_recent_transactions
from ledger.readmodels.webhooks import load_delivery_queue, load_queue_summary

PanelLoader = Callable[[AsyncSession], Awaitable[dict[str, object]]]


async def load_balances_context(session: AsyncSession) -> dict[str, object]:
    return {"balances": await load_balances(session)}


async def load_transactions_context(session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    transactions = await load_recent_transactions(
        session, settings.dashboard_recent_transactions_limit
    )
    return {"transactions": transactions}


async def load_reconciliation_context(session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    runs = await load_run_history(session, settings.dashboard_recent_runs_limit)
    findings = (
        await load_findings_for_run(session, runs[0].id, settings.dashboard_queue_limit)
        if runs
        else []
    )
    findings_summary = await load_findings_summary(session)
    return {"runs": runs, "findings": findings, "findings_summary": findings_summary}


async def load_webhooks_context(session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    summary = await load_queue_summary(session)
    deliveries = await load_delivery_queue(session, settings.dashboard_queue_limit)
    return {
        "summary": summary,
        "deliveries": deliveries,
        "max_attempts": settings.webhook_max_attempts,
        "max_reclaims": settings.webhook_max_reclaims,
    }


#: Order matters for nothing functional, but matches the panel order
#: `index.html` renders them in.
PANEL_LOADERS: dict[str, PanelLoader] = {
    "balances": load_balances_context,
    "transactions": load_transactions_context,
    "reconciliation": load_reconciliation_context,
    "webhooks": load_webhooks_context,
}

PANEL_NAMES: tuple[str, ...] = tuple(PANEL_LOADERS)
