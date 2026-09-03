"""OpenAPI metadata (SPEC.md §9, §12 Phase 7): tag descriptions and the
shared RFC 7807 `responses=` block every `/v1` router carries.

Security scheme registration itself is automatic -- `fastapi.security.
HTTPBearer` inside `ledger.api.auth.require_api_key` is what makes FastAPI
emit `components.securitySchemes.ApiKeyBearer` and a `security` requirement
on every operation whose dependency tree includes it, i.e. every `/v1`
route (via `V1_DEPENDENCIES`) and none of `/healthz`, `/readyz`, `/metrics`,
or `/dashboard/*`. `tests/unit/test_openapi.py` pins that split as an
invariant.
"""

from typing import Any

from ledger.schemas.problems import Problem

TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "health",
        "description": "Liveness and readiness probes. Unauthenticated, like /metrics.",
    },
    {
        "name": "accounts",
        "description": "Ledger accounts and their entry history (SPEC.md §2, §9).",
    },
    {
        "name": "transactions",
        "description": (
            "Balanced, multi-leg postings and reversals (SPEC.md §5). "
            "`POST /v1/transactions` and `POST .../reverse` accept an "
            "`Idempotency-Key` header (SPEC.md §6): retried with the same "
            "key and body, they replay the original response instead of "
            "posting twice."
        ),
    },
    {
        "name": "settlements",
        "description": "External settlement feed ingestion (SPEC.md §7).",
    },
    {
        "name": "reconciliation",
        "description": (
            "Three-pass matching against ingested settlements and bounded "
            "auto-resolution (SPEC.md §7). `POST .../runs` is idempotent."
        ),
    },
    {
        "name": "webhooks",
        "description": (
            "Outbox-backed, at-least-once webhook delivery (SPEC.md §8). "
            "A receiver may see the same `X-Ledgerline-Event-Id` more than "
            "once and must dedupe on it."
        ),
    },
    {"name": "admin", "description": "Operational invariant checks (SPEC.md §4, §9)."},
    {
        "name": "dashboard",
        "description": (
            "An HTML operator console outside `/v1` and outside API key auth "
            "(SPEC.md §12 Phase 6) -- it has no client contract to version."
        ),
    },
]

#: RFC 7807 responses every `/v1` operation can produce, keyed as
#: FastAPI's `responses=` expects. `404` and `201` are added per-route
#: where they apply, not here -- not every `/v1` operation can 404 or 201.
#:
#: Documented via `"model": Problem` alone, not a `"content"` override:
#: FastAPI renders that as an `application/json` media type in the
#: generated schema, which understates the real
#: `application/problem+json` content-type `ledger.api.errors` actually
#: sends (see each description below) -- combining `"model"` with a
#: `"content"` override produces a second, empty `application/problem+json`
#: entry instead of replacing the media type, which is worse than the
#: understatement.
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {
        "model": Problem,
        "description": "Missing, malformed, or invalid API key (application/problem+json).",
    },
    409: {
        "model": Problem,
        "description": (
            "A conflicting concurrent request or state change (application/problem+json)."
        ),
    },
    422: {
        "model": Problem,
        "description": (
            "Request failed validation or a domain invariant (application/problem+json)."
        ),
    },
    429: {
        "model": Problem,
        "description": (
            "Rate limit exceeded: 100 req/s, burst 200, per API key "
            "(application/problem+json, Retry-After header)."
        ),
    },
}

APP_DESCRIPTION = """\
An idempotent double-entry payments ledger and reconciliation engine.

**Auth**: every `/v1` route requires `Authorization: Bearer <key>`, minted via
`python -m ledger.admin.keys mint`. `/healthz`, `/readyz`, `/metrics`, and
`/dashboard/*` do not.

**Idempotency**: `POST /v1/transactions`, `POST .../reverse`, and
`POST /v1/reconciliation/runs` accept an `Idempotency-Key` header. A retry
with the same key and request body replays the original response
(`Idempotent-Replay: true`) instead of repeating the effect; the same key
with a *different* body is a `422 /errors/idempotency-key-reuse`.

**Webhooks** are at-least-once: a receiver may observe the same
`X-Ledgerline-Event-Id` more than once and must dedupe on it.

**Rate limits**: 100 req/s, burst 200, per API key. A `429` carries a
`Retry-After` header.

**Errors** are RFC 7807 `application/problem+json` with a stable `type`.
"""
