# Architecture

See `SPEC.md` for the full build specification. This document is a short orientation to the layering, updated as phases land.

## Layers (current: Phase 1)

- `ledger/models/` — SQLAlchemy 2.0 async ORM models, one module per table group. `ledger/models/__init__.py` imports every model class so `Base.metadata` is complete wherever it's needed (Alembic, tests).
- `ledger/db/` — engine (`engine.py`) and session factory (`session.py`). `get_session()` is the FastAPI dependency; it never commits — the caller (a route handler or service function) owns the transaction boundary.
- `ledger/config.py` — single `pydantic-settings` surface for all configuration, including values not yet used until later phases (idempotency TTL, reconciliation window/cutoff/threshold), so config never has to be re-plumbed.
- `ledger/observability/` — structured JSON logging (`structlog`) and a request-ID middleware that binds `request_id` into log context for the lifetime of each request.
- `ledger/api/` — FastAPI app. Phase 1 only wires `/healthz` (liveness) and `/readyz` (DB reachability + Alembic-head check).
- `migrations/` — Alembic, async-engine-driven. `0001_initial_schema` creates every table in the spec's data model up front, including the append-only trigger on `entries`.

## Not yet implemented (later phases, per `SPEC.md` §12)

`ledger/core/` (posting, invariants, money, idempotency), `ledger/reconciliation/`, `ledger/webhooks/`, `worker/`, `dashboard/` all exist as empty package stubs so later phases don't require restructuring — they carry no logic yet.
