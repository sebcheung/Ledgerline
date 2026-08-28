# Architecture

See `SPEC.md` for the full build specification. This document is a short orientation to the layering, updated as phases land.

## Layers (current: Phase 3)

- `ledger/models/` — SQLAlchemy 2.0 async ORM models, one module per table group. `ledger/models/__init__.py` imports every model class so `Base.metadata` is complete wherever it's needed (Alembic, tests). Indexes and partial-unique constraints are declared in `__table_args__` so they agree with what the migrations actually create — `alembic check` runs in CI specifically to catch the two from drifting.
- `ledger/db/` — engine (`engine.py`), session factory (`session.py`), and `errors.py` (identifying which DB constraint fired inside a wrapped `IntegrityError`). `get_session()` is the FastAPI dependency; it never commits — the caller (a route handler or service function) owns the transaction boundary.
- `ledger/core/` — the ledger's domain logic, framework-free (never imports `fastapi` or anything under `ledger/api`):
  - `money.py` — the `Money` value type (parse/format at the edges; posting itself works in plain `int` minor units).
  - `invariants.py` — `signed_delta` (the one place the debit/credit sign rule is expressed), `assert_transaction_balanced`, and the read-only verifiers `verify_account_derivability` / `verify_all_derivability` / `verify_global_balance`.
  - `posting.py` — `post_transaction` and `reverse_transaction`.
  - `idempotency.py` (Phase 3) — the SPEC.md §6 protocol: `canonical_hash` (fingerprinting), `claim_key`/`complete_key`/`release_key`/`load_key` (the claim/completion/release lifecycle). The one function in `ledger.core` that commits internally — see "Idempotency" below.
  - `errors.py` — the `LedgerError` hierarchy; every raiseable failure mode is a subclass carrying an RFC 7807 `error_type`/`title`/`status`, and (Phase 3) an optional `headers` mapping for cases like `Retry-After`.
- `ledger/schemas/` — Pydantic v2 request/response models. Validate field shape only (currency pattern, `amount > 0`, entry-count bounds); cross-entry semantics (balance, currency match, sufficient funds) are deliberately left to `ledger.core` so failures surface with the correct, stable type URI rather than a generic validation error.
- `ledger/webhooks/outbox.py` — `emit_event`, called from inside `post_transaction`/`reverse_transaction` in the same DB transaction as the ledger write. No fan-out yet (Phase 5).
- `ledger/observability/` — structured JSON logging (`structlog`) and a request-ID middleware that binds `request_id` into log context for the lifetime of each request.
- `ledger/api/` —
  - `deps.py` — `SessionDep`, and `get_idempotency_key` (reads and length-bounds the `Idempotency-Key` header; optional).
  - `idempotent.py` (Phase 3) — `get_idempotent_request` (the FastAPI dependency: builds the endpoint scope string and, if a key is present, the request fingerprint — no I/O) and `IdempotentRequest.run(execute_fn)` (called from inside each idempotent route; owns the claim/execute/complete-or-release sequence). See "Idempotency" below.
  - `errors.py` — RFC 7807 `application/problem+json` rendering; registers handlers for `LedgerError`, `RequestValidationError`, `StarletteHTTPException`, `IdempotentReplay` (Phase 3), and the unhandled-exception catch-all.
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

## Idempotency (Phase 3)

`POST /v1/transactions` and `POST /v1/transactions/{id}/reverse` both accept an optional `Idempotency-Key` header, implementing SPEC.md §6 end-to-end. Two commits are involved, deliberately:

1. **The claim.** `get_idempotent_request` (a FastAPI dependency) does no I/O — it only computes the endpoint scope string (`METHOD route-template`) and, if a key is present, the request fingerprint (`ledger.core.idempotency.canonical_hash` over `{"body", "path"}` — path params are folded in specifically so an empty-bodied route like `.../reverse` still fingerprints per-target). The claim itself — `claim_key`'s `INSERT ... ON CONFLICT DO NOTHING`, or a stale-lock reclaim — happens inside `IdempotentRequest.run`, called explicitly from the route body *after* FastAPI has already validated the request against its Pydantic model. `claim_key` commits immediately. This is the one function in `ledger.core` that does; every other function in the module obeys the caller-commits rule below. Committing here is what lets 20 concurrent duplicates observe each other's `in_progress` row and fail fast with a 409, instead of all queueing behind one Postgres speculative-insert wait for the full duration of the winner's posting.
2. **The completion.** If the claim says `EXECUTE`, `run` calls the route's `execute()` closure (the actual `post_transaction`/`reverse_transaction` call), then `complete_key` (an `UPDATE ... status='completed'`, no commit of its own), then commits. **This** is SPEC.md §6's "single commit at the end" — the ledger write and the key's completion share one transaction, so a crash between them leaves zero ledger rows and the key `in_progress`, and a later retry reclaims cleanly after the TTL (`IDEMPOTENCY_LOCK_TTL_SECONDS`, default 30).

If `execute()` raises `DuplicateTransaction` (the `transactions.idempotency_key` unique-constraint backstop firing because a stale-lock reclaim raced a still-live original), `run` rolls back and re-reads the key: completed with a matching fingerprint → replay; otherwise → 409. If `execute()` raises anything else, `run` rolls back and deletes the claim (`release_key`, fenced on the `locked_at` it wrote) rather than leaving it locked for the full TTL after a request that had no ledger effect.

A replayed response is served by raising `IdempotentReplay` from `run`, rendered by its own handler with `Idempotent-Replay: true`. The stored `response_body` is an envelope (`{"v", "body", "headers"}`, JSONB, no schema change) so a replay can carry `Location` without coupling the generic protocol to transaction-shaped responses.

## Transaction boundary

Route handler calls a `ledger.core` service function → the service never commits → the handler commits explicitly. `get_session()`'s `async with` block rolls back anything left open if a handler raises before committing. `ledger.core.idempotency.claim_key` (and its counterpart `release_key`) are the sole exceptions — see "Idempotency" above.

## Error contract

Every `LedgerError` subclass declares a stable `error_type` (an RFC 7807 `type` URI, kept relative exactly as SPEC.md §9 writes it), a `title`, an HTTP `status`, and (Phase 3) an optional `headers` mapping (e.g. `DuplicateTransaction`'s `Retry-After: 1`). `ledger/api/errors.py` renders these — and framework-level failures (validation errors, generic `HTTPException`, unhandled exceptions, and Phase 3's `IdempotentReplay`) — as `application/problem+json` (`IdempotentReplay` is the one exception rendered as a plain success body instead, since a replay isn't a failure). Spec-named URIs: `/errors/unbalanced-transaction` (422), `/errors/insufficient-funds` (422), `/errors/currency-mismatch` (422), `/errors/account-not-found` (404), `/errors/idempotency-conflict` (409, `DuplicateTransaction` — both the raw unique-constraint backstop *and*, via the module-local alias `IdempotencyConflict` in `ledger.core.idempotency`, the in-flight-lock conflict SPEC.md §6 describes; see `docs/DECISIONS.md` for why these share one URI), `/errors/idempotency-key-reuse` (422, Phase 3), `/errors/already-reversed` (409). Extensions: `/errors/transaction-not-found` (404), `/errors/suspense-account-exists` (409), `/errors/invalid-transaction` / `/errors/invalid-money` / `/errors/invalid-currency` (422), `/errors/invalid-cursor` (400), `/errors/validation-error` (422), `/errors/internal` (500), plus the generic `/errors/http-<status>` fallback for other framework HTTP errors; Phase 3 adds `/errors/idempotency-key-scope-conflict` (422), `/errors/idempotency-state` (500), `/errors/invalid-request-body` (400, unreachable through the API today).

## Pagination

`GET /v1/accounts/{id}/entries` and `GET /v1/transactions` use an opaque, versioned, unsigned keyset cursor over `(created_at DESC, id DESC)` — never OFFSET. The `id` tiebreak is load-bearing: Postgres's `now()` is transaction-start time, so every entry written by one `post_transaction` call shares an identical `created_at`, and a `created_at`-only cursor would skip or duplicate rows at multi-leg transaction boundaries.

## Outbox

`transaction.posted` and `transaction.reversed` events are written to `outbox_events` inside the same DB transaction as the ledger write, starting in Phase 2 — this is what gives the outbox pattern its atomicity guarantee, even though the dispatcher (fan-out to `webhook_deliveries`, HTTP delivery with retries) doesn't exist until Phase 5. A reversal emits *both* events: `transaction.posted` for the reversal transaction itself (it is a real posting) and `transaction.reversed` describing the original.

## Not yet implemented (later phases, per `SPEC.md` §12)

`ledger/reconciliation/`, `ledger/webhooks/dispatcher.py` and `signing.py`, `worker/`, `dashboard/` all exist as empty package stubs so later phases don't require restructuring — they carry no logic yet.
