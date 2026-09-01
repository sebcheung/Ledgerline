# Ledgerline

Idempotent payments ledger and reconciliation engine. See [`SPEC.md`](SPEC.md) for the full build specification and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) / [`docs/DECISIONS.md`](docs/DECISIONS.md) for current implementation notes.

## Development

```bash
cp .env.example .env          # edit if needed
docker compose up -d db
pip install -e ".[dev]"
alembic upgrade head
uvicorn ledger.api.main:app --reload
```

Run tests (spins up a throwaway Postgres via testcontainers if `DATABASE_URL` isn't set):

```bash
pytest
```

## Delivery semantics

Webhook delivery is **at-least-once**: a worker crash mid-delivery is recovered by a stale-claim sweep that returns the delivery to `pending` for redelivery. Receivers must dedupe on the `X-Ledgerline-Event-Id` header.

## Reconciliation semantics

The ledger is authoritative: a settlement line the ledger doesn't recognize can be automatically absorbed (via a suspense/clearing adjustment), but a ledger transaction the feed doesn't confirm is never assumed wrong (`missing_settlement` stays `unresolved` pending manual review, never auto-reversed). Auto-resolution is bounded — only differences at or below `RECON_AUTO_RESOLVE_THRESHOLD_MINOR` (default $5.00) are posted automatically; anything larger requires an explicit `POST /v1/reconciliation/findings/{id}/resolve`. Running a reconciliation over an unchanged window is safe to repeat: it reports no new findings and never re-adjusts an already-resolved one.
