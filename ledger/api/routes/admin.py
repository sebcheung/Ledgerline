from typing import Any

from fastapi import APIRouter

from ledger.api.deps import SessionDep
from ledger.core.invariants import verify_all_derivability, verify_global_balance

router = APIRouter()


@router.get("/admin/verify", summary="Verify global balance and per-account derivability")
async def verify(session: SessionDep) -> dict[str, Any]:
    """Runs invariants 3 (derivability) and 7 (global balance).

    Always returns 200: a monitoring endpoint that 500s on the exact
    condition it exists to detect is useless. Alerting should key on the
    `ok` field, not the HTTP status.
    """
    global_report = await verify_global_balance(session)
    account_reports = await verify_all_derivability(session)

    return {
        "ok": global_report.ok and all(r.ok for r in account_reports),
        "global_balance": {
            "ok": global_report.ok,
            "by_currency": [
                {
                    "currency": c.currency,
                    "debit_total": c.debit_total,
                    "credit_total": c.credit_total,
                    "ok": c.ok,
                }
                for c in global_report.by_currency
            ],
        },
        "accounts": [
            {
                "account_id": str(r.account_id),
                "currency": r.currency,
                "stored_balance": r.stored_balance,
                "derived_balance": r.derived_balance,
                "stored_entry_count": r.stored_entry_count,
                "derived_entry_count": r.derived_entry_count,
                "ok": r.ok,
            }
            for r in account_reports
        ],
    }
