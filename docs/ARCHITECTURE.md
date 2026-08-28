# Architecture

See `SPEC.md` for the full build specification. This document is a short orientation to the layering, updated as phases land.

## Layers (current: Phase 2)

- `ledger/models/` — SQLAlchemy 2.0 async ORM models, one module per table group. `ledger/models/__init__.py` imports every model class so `Base.metadata` is complete wherever it's needed (Alembic, tests). Indexes and partial-unique constraints are declared in `__table_args__` so they agree with what the migrations actually create — `alembic check` runs in CI specifically to catch the two from drifting.
- `ledger/db/` — engine (`engine.py`), session factory (`session.py`), and `errors.py` (identifying which DB constraint fired inside a wrapped `IntegrityError`). `get_session()` is the FastAPI dependency; it never commits — the caller (a route handler or service function) owns the transaction boundary.
- `ledger/core/` — the ledger's domain logic, framework-free (never imports `fastapi` or anything under `ledger/api`):
  - `money.py` — the `Money` value type (parse/format at the edges; posting itself works in plain `int` minor units).
  - `invariants.py` — `signed_delta` (the one place the debit/credit sign rule is expressed), `assert_transaction_balanced`, and the read-only verifiers `verify_account_derivability` / `verify_all_derivability` / `verify_global_balance`.
  - `posting.py` — `post_transaction` and `reverse_transaction`.
  - `errors.py` — the `LedgerError` hierarchy; every raiseable failure mode is a subclass carrying an RFC 7807 `error_type`/`title`/`status`.
- `ledger/schemas/` — Pydantic v2 request/response models. Validate field shape only (currency pattern, `amount > 0`, entry-count bounds); cross-entry semantics (balance, currency match, sufficient funds) are deliberately left to `ledger.core` so failures surface with the correct, stable type URI rather than a generic validation error.
- `ledger/webhooks/outbox.py` — `emit_event`, called from inside `post_transaction`/`reverse_transaction` in the same DB transaction as the ledger write. No fan-out yet (Phase 5).
- `ledger/observability/` — structured JSON logging (`structlog`) and a request-ID middleware that binds `request_id` into log context for the lifetime of each request.
- `ledger/api/` —
  - `deps.py` — `SessionDep`, and `get_idempotency_key` (reads the `Idempotency-Key` header and passes it straight to the posting service; Phase 3 replaces this dependency with the full claim/replay protocol without touching the service signature).
  - `errors.py` — RFC 7807 `application/problem+json` rendering; registers handlers for `LedgerError`, `RequestValidationError`, `StarletteHTTPException`, and the unhandled-exception catch-all.
  - `routes/accounts.py`, `routes/transactions.py`, `routes/admin.py` — the Phase 2 API surface (see below).
  - `health.py` — `/healthz` (liveness) and `/readyz` (DB reachability + Alembic-head check).
- `migrations/` — Alembic, async-engine-driven. `0001_initial_schema` created every table, index, native enum, and the append-only trigger on `entries`; Phase 2 needed no new migration (every candidate was evaluated and rejected — see `docs/DECISIONS.md`).

## Posting concurrency model

Postgres default READ COMMITTED, with an explicit `SELECT ... FOR UPDATE OF account_balances` over the distinct accounts touched by a transaction, rows acquired in **ascending `account_id` order**. Postgres places the `LockRows` plan node above `Sort`, so rows are locked in `ORDER BY` order regardless of the order the caller named accounts in — this is what makes two concurrent postings structurally unable to deadlock. A reversal additionally locks the original `transactions` row first, via a separate lock family that is always acquired before any balance lock, so reversals cannot introduce a new deadlock cycle either. `tests/integration/test_concurrency.py` includes a *positive control* (`test_control_unordered_locking_does_deadlock`) that deliberately locks out of order and asserts it deadlocks, so the paired "no deadlock" assertion is falsifiable rather than trivially true.

## Where each invariant is enforced

| # | Invariant | Enforcement |
|---|---|---|
| 1 | Balance | `assert_transaction_balanced`, called pre-flush in `post_transaction` |
| 2 | Immutability | DB trigger `entries_no_update`; posting uses Core selects/inserts only, so no `Entry` ORM entity is ever in the identity map to be autoflushed |
| 3 | Derivability | Maintained by construction (entries insert + balance update happen in one DB transaction under one lock); verified on demand by `verify_account_derivability`/`verify_all_derivability` |
| 4 | Currency consistency | `post_transaction`, checked against the account row returned by the `FOR UPDATE` join |
| 5 | Sign constraint | `post_transaction`, checked against the *locked* balance |
| 6 | Reversal symmetry | By construction (the mirrored entry set is the exact negation) + the `uq_transactions_reversal_of` partial unique index |
| 7 | Global balance | Follows from 1 + 2; verified on demand by `verify_global_balance`, grouped per currency |

## Transaction boundary

Route handler calls a `ledger.core` service function → the service never commits → the handler commits explicitly. `get_session()`'s `async with` block rolls back anything left open if a handler raises before committing.

## Error contract

Every `LedgerError` subclass declares a stable `error_type` (an RFC 7807 `type` URI, kept relative exactly as SPEC.md §9 writes it), a `title`, and an HTTP `status`. `ledger/api/errors.py` renders these — and framework-level failures (validation errors, generic `HTTPException`, unhandled exceptions) — as `application/problem+json`. Spec-named URIs: `/errors/unbalanced-transaction` (422), `/errors/insufficient-funds` (422), `/errors/currency-mismatch` (422), `/errors/account-not-found` (404), `/errors/idempotency-conflict` (409, also used for the Phase 2 `DuplicateTransaction` backstop), `/errors/already-reversed` (409). Extensions added in Phase 2: `/errors/transaction-not-found` (404), `/errors/suspense-account-exists` (409), `/errors/invalid-transaction` / `/errors/invalid-money` / `/errors/invalid-currency` (422), `/errors/invalid-cursor` (400), `/errors/validation-error` (422), `/errors/internal` (500), plus the generic `/errors/http-<status>` fallback for other framework HTTP errors.

## Pagination

`GET /v1/accounts/{id}/entries` and `GET /v1/transactions` use an opaque, versioned, unsigned keyset cursor over `(created_at DESC, id DESC)` — never OFFSET. The `id` tiebreak is load-bearing: Postgres's `now()` is transaction-start time, so every entry written by one `post_transaction` call shares an identical `created_at`, and a `created_at`-only cursor would skip or duplicate rows at multi-leg transaction boundaries.

## Outbox

`transaction.posted` and `transaction.reversed` events are written to `outbox_events` inside the same DB transaction as the ledger write, starting in Phase 2 — this is what gives the outbox pattern its atomicity guarantee, even though the dispatcher (fan-out to `webhook_deliveries`, HTTP delivery with retries) doesn't exist until Phase 5. A reversal emits *both* events: `transaction.posted` for the reversal transaction itself (it is a real posting) and `transaction.reversed` describing the original.

## Not yet implemented (later phases, per `SPEC.md` §12)

`ledger/core/idempotency.py` (Phase 3 — the full claim/replay/stale-lock-reclamation protocol; the unique-constraint backstop it will sit on top of is already live), `ledger/reconciliation/`, `ledger/webhooks/dispatcher.py` and `signing.py`, `worker/`, `dashboard/` all exist as empty package stubs so later phases don't require restructuring — they carry no logic yet.
