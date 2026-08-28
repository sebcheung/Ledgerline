# Design Decisions

Each entry: what was chosen, what was rejected, why.

## Primary key generation: DB-side `gen_random_uuid()`

**Chosen:** Postgres 16's built-in `gen_random_uuid()` as the `server_default` for every table's `id` column.
**Rejected:** app-side `uuid.uuid4()` generation before INSERT.
**Why:** keeps ID generation atomic with the insert and avoids any app/DB split-brain about what the "real" ID is. The one thing app-side generation buys you — knowing the ID before the INSERT executes — isn't needed anywhere in Phase 1; where a later phase needs a client-known identifier up front (e.g. the idempotency key), that's already a separate value from the row's primary key.

## Enum representation: native Postgres enums

**Chosen:** native Postgres `ENUM` types (`CREATE TYPE ...`), mapped via `sqlalchemy.Enum(native_enum=True, create_type=False)`, with the type creation owned explicitly by the Alembic migration.
**Rejected:** `text` column + `CHECK (col IN (...))`.
**Why:** DB-level enforcement, self-documenting schema, cheaper storage.
**Trade-off:** adding a new enum value later requires `ALTER TYPE ... ADD VALUE`, which PostgreSQL will not run inside the same transaction as other DDL in a single Alembic revision — a future migration that adds a value needs to run outside an explicit transaction block. Accepted for a small, well-understood value set defined directly from the spec.

## `entries.currency` vs cross-table validation

**Chosen:** `entries` carries its own `currency` column (per spec §3), and consistency between `entries.currency` and `accounts.currency` is enforced at the application level in `ledger/core/posting.py` (Phase 2), not via a Postgres trigger.
**Rejected:** a validating trigger on `entries` that joins to `accounts` to check currency match.
**Why:** the invariant is naturally checked as part of the posting transaction anyway (the posting algorithm already loads the account row under `FOR UPDATE`), so a DB trigger would be redundant enforcement for a check the app already has to do correctly. Deferred rather than built blind, so the trigger (if ever added) validates the real invariant as implemented, not a guess made before posting logic exists.

## API key storage: hash only

**Chosen:** `api_keys.key_hash` stores a SHA-256 hex digest of the key; the raw key is never persisted.
**Why:** standard practice — a leaked database dump must not yield usable credentials.

## Build backend: hatchling, no src/ layout

**Chosen:** `hatchling` build backend with `ledger/`, `worker/`, `dashboard/` as top-level packages (no `src/` layout).
**Rejected:** Poetry or uv (neither installed in the dev environment) and a `src/` layout.
**Why:** zero-config for a flat multi-package repo, works with plain `pip install -e ".[dev]"`, and matches the repo tree given in the spec exactly.

## Migration runner: async engine wrapped with `run_sync`

**Chosen:** `migrations/env.py` builds an `AsyncEngine` and drives migrations via `connection.run_sync(...)` inside `asyncio.run(...)`.
**Rejected:** a separate sync driver (psycopg2) just for Alembic.
**Why:** stays on the asyncpg driver end-to-end; one DB driver dependency, not two.

---

# Phase 2 — Ledger Core

## Minor-unit exponent: per-currency table, default 2

**Chosen:** `ledger/core/money.py` ships a small exception table (`MINOR_UNIT_EXPONENT`) for currencies whose exponent isn't 2 — zero-decimal (JPY, KRW, ...), three-decimal (BHD, KWD, TND, ...), four-decimal (CLF, UYW) — defaulting everything else to 2, plus a `^[A-Z]{3}$` format check.
**Rejected:** a fixed 2-decimal assumption (silently corrupts JPY amounts by 100x, three-decimal currencies by 10x), and a full vendored ISO 4217 registry.
**Why:** SPEC.md §3 says "minor units (cents)" but also "ISO 4217" — the two are only consistent for 2-decimal currencies. A full registry is unjustified because multi-currency FX conversion is an explicit non-goal (§1); the exception table is correct for every currency that actually differs, at the cost of one small dict.
**Trade-off:** an unknown-but-well-formed 3-letter code silently gets exponent 2.

## `Money` is an edge type, not the posting currency

**Chosen:** `Money` exists to parse/format amounts and to express invariant checks readably. The posting hot path (`post_transaction`) works entirely in plain `int` minor-unit deltas keyed by `account_id`.
**Rejected:** constructing `Money` objects throughout posting.
**Why:** by the time posting computes deltas, step 1 has already proven every entry shares one currency — constructing `Money` there would add allocations without adding safety.

## No rounding in `Money.from_decimal_string`

**Chosen:** a decimal string with more fractional digits than the currency's exponent supports raises `InvalidMoney` rather than rounding.
**Rejected:** implicit half-up/banker's rounding.
**Why:** silently rounding on a money boundary is the classic ledger correctness bug; the caller must be explicit about the amount it means.

## Domain errors in `ledger/core/errors.py`, HTTP rendering in `ledger/api/errors.py`

**Chosen:** every failure mode `ledger.core` can raise is a `LedgerError` subclass carrying `error_type`/`title`/`status`/`extra`; `ledger/api/errors.py` renders these (and framework exceptions) as RFC 7807 `application/problem+json`.
**Why:** keeps the dependency arrow one-way (`api -> core`, never the reverse), so `ledger.reconciliation` and `worker` (later phases) can raise and catch the same exception types without importing FastAPI. `type` URIs are kept exactly as SPEC.md §9 writes them — relative, not resolved against a configurable base URL — because "stable" means not varying per deployment host.
**`DuplicateTransaction` maps to the existing `/errors/idempotency-conflict` (409)** rather than a new URI: SPEC.md §9's table already has this slot, and §6 makes this exception reachable whenever no idempotency layer intercepts it first (true throughout Phase 2, and true in Phase 3 whenever the `idempotency_keys` row is missing or still `in_progress`).

## Posting uses SQLAlchemy Core, not the ORM unit of work

**Chosen:** `post_transaction`/`reverse_transaction` use `insert()`/`select()`/`update()` with `RETURNING`, never `session.add()`.
**Why:** the models carry no `relationship()`s; a `SAVEPOINT` rollback (see below) leaves no pending ORM instances behind to be accidentally re-flushed, because Core never puts one in the session; and it keeps the locked `account_balances` rows out of the identity map, so there is no staleness hazard under `expire_on_commit=False`.
**Consequence:** `post_transaction`/`reverse_transaction` return a frozen `PostedTransaction` dataclass, not the ORM `Transaction` (a deviation from SPEC.md §5's literal type annotation) — there is no attached ORM instance to hand back, the ORM model has no `entries` collection, and both the API response and the outbox payload need the entries alongside the transaction fields.

## Ordered locking: `FOR UPDATE OF account_balances`, ascending `account_id`, inner join to `accounts`

**Chosen:** `SELECT ... FROM account_balances JOIN accounts ... WHERE account_balances.account_id IN (...) ORDER BY account_balances.account_id FOR UPDATE OF account_balances`.
**Why:** Postgres places the `LockRows` plan node *above* `Sort`, so rows are locked in `ORDER BY` order — this is what makes ordered acquisition real and makes deadlock between two concurrent postings structurally impossible, independent of the order their callers named accounts in. `FOR UPDATE OF account_balances` (not both tables) locks only the contended rows; locking `accounts` too would serialize unrelated reads for no benefit, since `accounts` is read-mostly. The join must be `INNER`, not `LEFT`: Postgres refuses `FOR UPDATE` on the nullable side of an outer join.
**Verified, not just asserted:** `tests/integration/test_concurrency.py::test_control_unordered_locking_does_deadlock` deliberately locks in *request* order and asserts it deadlocks (SQLSTATE `40P01`) under the same harness, so the paired "no deadlock" test is falsifiable rather than trivially passing.

## Missing `account_balances` row: distinguish, then self-heal

**Chosen:** if a `account_id` in the lock select comes back missing, a second (sad-path-only) query checks whether the account itself exists. Genuinely absent → `AccountNotFound`. Exists but has no balance row → `INSERT ... ON CONFLICT DO NOTHING`, logged at `warning`, then the lock select re-runs once.
**Rejected:** treating "no balance row returned" as `AccountNotFound` unconditionally (masks a real data-integrity gap as a client error), and unconditionally upserting a balance row before every posting (an extra write on the hot path for a case that should never occur).
**Why unreachable in practice:** `POST /v1/accounts` creates the account and its balance row in the same DB transaction (`ledger/api/routes/accounts.py`), so this path only fires if that invariant is ever violated — which is exactly when a loud warning is wanted.

## `SAVEPOINT` around the transaction insert

**Chosen:** the `INSERT INTO transactions` that can trigger the `idempotency_key`/`reversal_of` unique-violation backstop runs inside `session.begin_nested()`.
**Why:** an `IntegrityError` aborts the underlying Postgres transaction; without a savepoint, every later statement on that connection fails with `25P02` until rollback. Phase 3's idempotency layer needs to `SELECT idempotency_keys` immediately after `DuplicateTransaction` fires, and Phase 4's reconciliation resolver posts several transactions in a loop — neither can tolerate the session being poisoned by one expected failure.

## Constraint identification: structured asyncpg attribute, with a substring fallback

**Chosen:** `ledger/db/errors.py::constraint_name_of` reads `constraint_name` off `exc.orig` and `exc.orig.__cause__` (asyncpg's `UniqueViolationError`, as wrapped by SQLAlchemy's asyncpg DBAPI shim), via `getattr(..., default)` rather than `# type: ignore`. A substring match on the exception text is a last resort.
**Rejected:** matching on `str(exc)` alone (brittle across driver/version combinations), and pre-checking for a duplicate with a `SELECT` before inserting (racy — the unique constraint is the actual guarantee, per SPEC.md §6, not an optimization).
**Verified:** `tests/integration/test_posting.py::test_structured_constraint_detection_fires` asserts the structured path is what actually fires, so the fallback can never silently become the real mechanism.

## Three independent guards against double reversal

**Chosen:** (1) `SELECT ... FOR UPDATE` on the original transaction row at the start of `reverse_transaction`; (2) a compare-and-swap `UPDATE ... WHERE status = 'posted'` with a `rowcount` check; (3) the DB's `uq_transactions_reversal_of` partial unique index.
**Why:** each guard covers a different failure mode — (1) serializes concurrent reversal attempts, (2) catches a status change that somehow happened without the lock, (3) is the last-resort DB-level backstop, the same pattern as the idempotency-key constraint.

## Reversal carries `external_ref = NULL`

**Chosen:** `reverse_transaction` never copies the original transaction's `external_ref` onto the reversal.
**Why:** Phase 4's pass-1 exact matcher keys on `(external_ref, amount, currency)`; carrying the ref forward would make one settlement line appear to match two ledger transactions.

## Reversal of a reversal is allowed

**Chosen:** no special-casing prevents reversing a transaction that is itself a reversal.
**Why:** `uq_transactions_reversal_of` already prevents reversing the *same* transaction twice; invariant 6 (reversal symmetry) holds independently for each (original, reversal) pair, so a chain of reversals is just a chain of independently-valid pairs. SPEC.md is silent here; forbidding it would be a policy choice layered on top of the ledger, not an invariant the ledger itself needs to enforce.

## Invariant 5 has no reversal exemption

**Chosen:** reversing a transaction is subject to the sign constraint exactly like any other posting; if the mirrored entries would drive a non-`allow_negative` account negative, `reverse_transaction` raises `InsufficientFunds` and the original stays `posted`.
**Why:** invariants are absolute, not conditional on operation type. The operator's remedy for an "unreversible" transaction (funds already moved on) is a compensating transaction, not a ledger-level exception to the sign rule.

## Outbox rows are written in Phase 2; the dispatcher is Phase 5

**Chosen:** `post_transaction` and `reverse_transaction` write `outbox_events` rows (`transaction.posted`, `transaction.reversed`) inside the same DB transaction as the ledger write, from Phase 2 onward. `ledger/webhooks/outbox.py` has no fan-out logic yet — fan-out to `webhook_deliveries` and HTTP delivery are Phase 5.
**Why:** the entire point of the outbox pattern (SPEC.md §8) is that the event and the ledger write commit atomically; deferring the write to Phase 5 would leave every pre-Phase-5 transaction eventless and would tempt a dual-write later. The *dispatcher* — the only genuinely Phase-5-shaped piece — is what's actually deferred.

## `idempotency_key` omitted from the webhook event payload

**Chosen:** `transaction_event_payload` excludes `idempotency_key`.
**Why:** it is a client-supplied request identifier with no reason to be handed to a third-party webhook endpoint.

## Cross-entry transaction semantics are validated in `ledger.core`, not Pydantic

**Chosen:** `TransactionCreate`/`EntryCreate` validate only field shape (currency pattern, `amount > 0`, `min 2 entries`). Single-currency, balance, account existence, currency match, and sufficient funds are all checked in `ledger.core.posting`/`invariants`.
**Rejected:** a Pydantic `model_validator` enforcing e.g. single-currency-across-entries.
**Why:** a validation error at the Pydantic layer would surface as a generic `/errors/validation-error` (422), not the stable, specific type URI SPEC.md §9 promises (`/errors/currency-mismatch`, `/errors/unbalanced-transaction`). Keeping these checks in `core` also means Phase 4's resolver (which calls `post_transaction` directly, not through the API) gets the same enforcement for free.

## Pagination cursor: opaque, versioned, unsigned, keyset on `(created_at, id)`

**Chosen:** `GET /v1/accounts/{id}/entries` and `GET /v1/transactions` paginate with a base64url-encoded `{"v", "t", "i"}` cursor, ordered `(created_at DESC, id DESC)`, fetching `limit + 1` rows to compute `has_more`.
**Rejected:** OFFSET pagination (skips or duplicates rows under the concurrent inserts a ledger accumulates continuously) and a `created_at`-only keyset.
**Why the `id` tiebreak is load-bearing, not defensive:** Postgres's `now()` is transaction-start time, constant for every statement in one DB transaction — every entry inserted by one `post_transaction` call shares an identical `created_at`. A `created_at`-only cursor would skip or duplicate rows at every multi-leg transaction boundary. `tests/integration/test_pagination.py::test_stable_order_under_identical_created_at` pins this.
**Why unsigned:** the cursor encodes only `(created_at, id)` values the client already received in the page it came from — nothing confidential or forgeable-with-consequence to protect. The version tag lets the encoding change later without silently mis-paginating an old stored cursor.

## `/readyz` error detail key: `status` renamed to `reason`

**Chosen:** `HTTPException(..., detail={"reason": ..., ...})` in `ledger/api/health.py`, and the DB-unreachable branch no longer echoes `str(exc)` to the client (logs it instead).
**Why:** the RFC 7807 problem document reserves `status` for the integer HTTP status code; `_http_exception_handler` merges an `HTTPException`'s dict `detail` in as top-level extension members, so a key literally named `status` would collide. Also stops leaking a raw driver exception string to API clients.

## Phase 2 ships zero migrations

**Chosen:** no `0002_*` migration. Every candidate index/constraint was evaluated and rejected: `ix_transactions_external_ref`/`ix_transactions_created_at` already serve the list filters and cursor ordering; `status` has only two values (a heap filter beats an index); the entries cursor's `id` tiebreak filters a tie-group at most one transaction wide, so the existing `(account_id, created_at)` index suffices; a `currency` format CHECK would be a third copy of a rule already enforced (as a typed 422) at both the Pydantic and `Money` layers.
**Deferred with a trigger condition:** a composite FK `account_balances(account_id, currency) -> accounts(id, currency)` (requiring `UNIQUE (id, currency)` on `accounts`) would make invariant 4's account↔balance half structurally impossible. Not added because `accounts.currency` has no UPDATE path today and `POST /v1/accounts` already writes both rows atomically — add this FK first if an account-mutation endpoint is ever introduced.
**Fixed instead:** the ORM models (`Account`, `Transaction`, `Entry`, `WebhookDelivery`) were missing `Index(...)` declarations matching the indexes migration `0001` creates via raw `op.create_index` — a pre-existing Phase 1 gap that Phase 2's new `alembic check` CI step caught immediately. Declared them in `__table_args__` so model metadata and migration truth agree; no schema change, no new migration.

## Concurrency tests use `asyncio.gather` over tasks, not OS threads

**Chosen:** the "50 threads" contention test and the deadlock-order test both use `asyncio.gather` over N tasks, each with its own session from a `NullPool` engine (`concurrency_engine` fixture), verified via `pg_backend_pid()` to confirm N genuinely distinct Postgres backends.
**Rejected:** literal OS threads.
**Why:** the property under test is N simultaneous *open Postgres transactions* contending for one locked row, not N OS threads specifically. asyncpg connections are bound to the event loop that created them, so real threads would require either N separate event loops (no additional value over N tasks on one loop) or a sync driver this project doesn't install. `NullPool` is what actually matters: the default pool (5 + 10 overflow) would silently serialize a 50-way test and let it pass while proving nothing.

## Test isolation: `TRUNCATE ... RESTART IDENTITY CASCADE` before each test, not transaction rollback

**Chosen:** an opt-in `clean_database` fixture (made autouse per-directory in `tests/integration/conftest.py` and `tests/property/conftest.py`, not at the repo root, so unit tests never force a database into existence) truncates every ledger table via a dedicated function-scoped `admin_engine`, run at test *setup*.
**Rejected:** wrapping each test in an outer transaction and rolling back.
**Why:** the concurrency tests require genuinely separate, concurrently-open Postgres transactions taking real row locks — a shared outer transaction can't produce that contention. Truncation at setup (not teardown) means a crashed test can't poison the one after it, and a failure's rows remain for post-mortem inspection. `entries_no_update` is a row-level `BEFORE UPDATE OR DELETE` trigger, so it does not intercept `TRUNCATE` — pinned by `tests/integration/test_truncate_bypasses_trigger` — which is the one fact the whole design leans on.
**Gotcha fixed along the way:** `admin_engine` must be function-scoped, not session-scoped — pytest-asyncio gives each test its own event loop by default, and a session-scoped asyncpg engine hands out loop-bound connections to a later test's different loop, surfacing as an opaque `InterfaceError: another operation is in progress`.

---

# Phase 3 — Idempotency

## The claim commits separately from the crux commit

**Chosen:** `ledger.core.idempotency.claim_key` commits immediately on a fresh claim or a stale-lock reclaim. The ledger write and the key's `status='completed'` UPDATE (`complete_key`) then share a *second*, later commit, made by `IdempotentRequest.run`.
**Rejected:** one transaction covering the claim through completion.
**Why:** this reads like it contradicts SPEC.md §6's "the single commit at the end is the crux" and doesn't — that sentence is about which *two* things share a commit (the ledger write and the completion), not about the whole request being one transaction. Postgres's `INSERT ... ON CONFLICT DO NOTHING` uses speculative insertion: when it conflicts with a row from an *uncommitted* transaction, it blocks on that transaction's outcome. If the claim shared a transaction with the (potentially slow) `execute()` step, all 20 concurrent duplicates in SPEC.md §10's test would queue behind the winner's entire posting instead of observing the `in_progress` row and returning a fast 409 — the fault suite's "1 execution, 19 replays or 409s" would degenerate into 20 executions run one at a time, and the 30-second TTL would be unobservable. Committing the claim immediately is what makes conflicts cheap and stale locks reclaimable at all.

## `get_idempotent_request` does no I/O; the claim happens inside `run()`

**Chosen:** the FastAPI dependency only reads the (already-buffered) request body, path params, and route template to compute the fingerprint — no database access. `claim_key` is called from `IdempotentRequest.run`, invoked explicitly from inside the route body.
**Rejected:** claiming the key directly in the dependency, which is what SPEC.md §11's "a FastAPI dependency" suggests at face value.
**Why:** FastAPI resolves every dependency in a route's tree *before* it validates the endpoint's own Pydantic body model (`fastapi.dependencies.utils.solve_dependencies` runs the sub-dependency loop to completion, then validates `dependant.body_params`). A dependency that claimed the key would still claim it for a request whose body is syntactically valid JSON but fails `TransactionCreate` validation — and nothing would ever complete or release that claim, because the route body (and therefore `run()`) never executes for a 422. Deferring the claim into `run()` means it only happens once FastAPI has already accepted the request.
**Consequence:** the dependency is `get_idempotent_request` (the claim/replay/reclaim machinery lives entirely in `ledger.core.idempotency` and `IdempotentRequest.run`), not a single opaque dependency as §11's phrasing implies — but `run(execute_fn)` matches §6's own `handle(request, endpoint, key, execute_fn)` signature exactly.

## Endpoint scope is the route template; path params are folded into the fingerprint

**Chosen:** `endpoint = f"{method} {route.path}"` (e.g. `POST /v1/transactions/{transaction_id}/reverse`), and the fingerprint hashes `{"body": ..., "path": {name: str(value), ...}}`, not the body alone.
**Rejected:** hashing `request.body` alone, which is what SPEC.md §6 literally says.
**Why:** `POST /v1/transactions/{id}/reverse` has no body at all — a body-only fingerprint would be identical for every reversal a client ever makes with that route, so a stale-lock reclaim could silently replay (or, worse, re-execute) against a *different* transaction than the one the key was first used for. Folding path params into the fingerprint, while keeping the concrete resource id out of `endpoint`, keeps `(key, endpoint)` scoping bounded by route count while still disambiguating targets. Pinned by `tests/faults/test_idempotency_reuse.py::test_same_key_on_reverse_for_two_different_transactions_is_key_reuse`.

## Canonical JSON drops `null` object members only

**Chosen:** `canonical_json`'s null-dropping pre-pass recurses into `dict`s and `list`s but only removes `None`-valued *object* members; `None` values inside arrays are preserved.
**Rejected:** dropping `None` wherever it appears, including array elements.
**Why:** SPEC.md §6 says "drop nulls" without qualification, but dropping array elements shifts indices — `[1, null, 2]` would canonicalize identically to `[1, 2]`, which are different requests. Object-member dropping is safe because `{"a": 1, "b": null}` and `{"a": 1}` really do mean the same request (an omitted optional field and an explicit null are equivalent in this API's schemas).

## Replayed responses are stored as a versioned envelope, not the bare body

**Chosen:** `response_body` (JSONB, an existing column — no migration) stores `{"v": 1, "body": ..., "headers": ...}`, serialized with `fastapi.encoders.jsonable_encoder` so it matches byte-for-byte what FastAPI's own response serialization would produce.
**Rejected:** storing the bare response body and deriving headers like `Location` from `body["id"]` at replay time.
**Why:** `Location` (and any other header a route sets) has to survive a replay, and SPEC.md §6 only describes storing `response_body`/`response_status`. Deriving `Location` from the body would couple the generic idempotency layer to transaction-shaped responses and break for Phase 4's `POST /v1/reconciliation/runs`, whose response has no analogous `id`-based URL. The `"v"` tag lets the envelope shape change later without misreading a row written by an older deploy.

## `DuplicateTransaction` keeps sole ownership of `/errors/idempotency-conflict`

**Chosen:** SPEC.md §6's "409 IdempotencyConflict" (an in-flight lock, not yet stale) is rendered as the *existing* `DuplicateTransaction` class and its existing URI. `ledger.core.idempotency` imports it under the alias `from ledger.core.errors import DuplicateTransaction as IdempotencyConflict`, so the protocol code reads in SPEC's vocabulary without declaring a second `LedgerError` subclass.
**Rejected:** a distinct `IdempotencyConflict` class with its own URI (which would violate `tests/unit/test_error_catalog.py::test_error_types_are_unique` unless `DuplicateTransaction` moved to a different URI first — considered and rejected as unnecessary churn).
**Why:** SPEC.md §9's error table has exactly one 409 idempotency slot, and from a client's perspective the raw unique-constraint backstop and the in-flight-lock conflict mean the identical thing: retry. `DuplicateTransaction` now also carries `headers = {"Retry-After": "1"}` (via the new `LedgerError.headers`/`problem_headers()` machinery, additive to `_ledger_error_handler`), which applies uniformly regardless of which code path raised it.

## The claim is released, not left locked, when `execute_fn` raises a non-duplicate error

**Chosen:** `IdempotentRequest.run` catches any exception other than `DuplicateTransaction`, rolls back, calls `release_key` (a `DELETE` fenced on the `locked_at` this claim wrote), and re-raises.
**Rejected:** leaving the key `in_progress` until the TTL expires (SPEC.md §6 doesn't describe this case at all — its `execute_fn` is assumed to succeed or raise `DuplicateTransaction`).
**Why:** if `execute_fn` raises e.g. `InsufficientFunds`, the ledger write never happened — there's nothing to be idempotent about. Leaving the key locked would force a client that corrects a typo and retries immediately to wait out the full 30-second TTL for no reason. `release_key`'s fence on `locked_at` matters the other way: a slow original failing at T+40s must not delete a *different* request's live reclaimed claim out from under it. `complete_key` carries no equivalent fence, because the `transactions.idempotency_key` unique constraint already guarantees at most one caller ever reaches it successfully.

## Staleness is evaluated with the Postgres clock, not the app clock

**Chosen:** `claim_key`'s conflict-branch `SELECT` computes `now() - locked_at > make_interval(secs => :ttl)` in SQL.
**Rejected:** comparing `row.locked_at` against `datetime.now(UTC)` in Python, which is what SPEC.md §6's pseudocode (`now() - row.locked_at < LOCK_TTL`) reads as if written for a single-process, single-clock system.
**Why:** `locked_at` is written by the database server's `now()`; comparing it against an application host's clock introduces skew that could reclaim a lock early (or late) under any clock drift between app and DB hosts — a correctness-relevant gap for a lock whose entire purpose is bounding how long a reclaim can jump the queue.

## Phase 3 ships zero migrations

**Chosen:** no `0002_*` migration. `idempotency_keys` (all columns SPEC.md §6 needs), its `idempotency_status` enum, and `transactions.idempotency_key UNIQUE` all already exist from `0001_initial_schema`.
**Why no new index:** every Phase 3 access to `idempotency_keys` is a primary-key lookup (`WHERE key = :key`); there is no scan over `status` or `locked_at` anywhere, so an index on either would never be chosen by the planner and would only cost write amplification on the busiest small table in the system.
**Deferred with a trigger condition:** `idempotency_keys` grows without bound — one row per idempotent request, forever, with no expiry. A retention job (`DELETE ... WHERE status = 'completed' AND created_at < now() - interval '30 days'`) plus `ix_idempotency_keys_created_at` should land together, once there's an operational surface (Phase 7, alongside `/metrics` and deploy) to run the sweep from — building the index first would be dead weight until something uses it.

## Idempotency metrics are structured log events for now

**Chosen:** `idempotency.claimed` / `.replayed` / `.conflict` / `.reclaimed` / `.released` / `.duplicate_backstop` are logged via the existing `structlog` setup at the points `IdempotentRequest.run` and `claim_key` already touch.
**Rejected:** wiring up `idempotency_replays_total` / `idempotency_conflicts_total` now, as named in SPEC.md §9.
**Why:** there is no `/metrics` endpoint or metrics registry until Phase 7 (SPEC.md §12); adding counters now means standing up that infrastructure early for two metrics that Phase 7 will want to define alongside everything else. The structured log events are the Phase 7 wiring points, named to match the metrics they'll eventually back.
