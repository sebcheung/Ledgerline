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

---

# Phase 4 — Settlements + Reconciliation

## `accounts.is_clearing`, not a config map of currency -> account id

**Chosen:** a new `accounts.is_clearing` boolean, one per currency (`uq_accounts_clearing_per_currency`, mirroring `uq_accounts_suspense_per_currency` exactly), is the resolver's "implied asset account" for `unexpected_settlement`/`amount_mismatch` adjustments (SPEC.md §7 names this account but defines no column for it).
**Rejected:** a `RECON_CLEARING_ACCOUNTS: dict[str, uuid.UUID]` setting.
**Why:** a config map can point at a deleted or wrong-currency account with no DB-level guard; `is_clearing` gets the same enforcement `is_suspense` already has for free. Two more guards close the gaps a bare boolean alone would leave open: `CHECK (NOT is_clearing OR type = 'asset')` (a clearing account is an asset account by definition) and `CHECK (NOT (is_suspense AND is_clearing))` (one account playing both roles would debit and credit itself in the `unexpected_settlement` adjustment -- `post_transaction` would accept that silently as a balanced, meaningless zero-effect posting).
**Operational requirement, not a resolver code path:** both the suspense and clearing account for a currency must be created with `allow_negative=true`, or `InsufficientFunds` strands nearly every adjustment unresolved. `scripts/seed.py` creates them that way; this is written down here because nothing in the schema enforces it.

## Manual resolve is action-based: `post_adjustment` | `suppress`

**Chosen:** `POST /v1/reconciliation/findings/{id}/resolve` takes `{"action": "post_adjustment" | "suppress", "note": ...}`. `post_adjustment` is valid only for `unexpected_settlement`/`amount_mismatch` (422 `InvalidFindingResolution` otherwise -- `in_flight`/`duplicate_settlement` already resolved themselves, `missing_settlement`/`currency_mismatch` have no delta to move) and reuses the exact ledger effect `resolve()` would have posted automatically, bypassing the auto-resolve threshold. `suppress` is valid for any still-`unresolved` finding and has no ledger effect.
**Why not idempotent:** SPEC.md §6's idempotent-endpoint list does not include this route, and it posts real money on an explicit, one-shot operator action -- unlike the run endpoint, there is no legitimate reason to retry it with the same key. Guarded instead by `SELECT ... FOR UPDATE` on the finding row plus a compare-and-swap `UPDATE ... WHERE resolution = 'unresolved'` with a `rowcount` check -- the same layered-guards shape this file already uses for double-reversal (Phase 2, "Three independent guards against double reversal"). A `rowcount == 0` raises the new `FindingAlreadyResolved` (409).
**Defense in depth:** the adjustment it posts uses the deterministic `recon-adjust:{finding_id}` idempotency key (see below), so even a bug in the CAS guard could not double-post.

## Concurrent runs: `pg_try_advisory_xact_lock`, acquired inside the idempotent `execute()` closure

**Chosen:** a single, literal, checked-in `bigint` lock key (`zlib.crc32(b"ledgerline:reconciliation_run")` -- never Python's `hash()`, which `PYTHONHASHSEED` randomizes per process and would let two workers take different locks for what must be one global lock), acquired via `pg_try_advisory_xact_lock` as the first thing `execute_run` does. Failure raises the new `ReconciliationRunInProgress` (409, `Retry-After: 5`; a genuinely new URI slot -- `DuplicateTransaction` owns `/errors/idempotency-conflict` exclusively, see the Phase 3 entry below).
**Rejected:** a partial unique index on `reconciliation_runs (true) WHERE status = 'running'`.
**Why:** the lock must be taken *inside* `IdempotentRequest.run`'s `execute()` closure, not before it -- `claim_key` commits (Phase 3's "the claim commits separately from the crux commit"), and an `xact`-scoped advisory lock acquired before that commit would be released by it, defeating the whole point. Taken inside `execute()`, the lock is released by `run`'s own commit or rollback with nothing to sweep after a crash -- a `status='running'` unique index would instead leave a permanently stuck row behind a crashed run, needing a separate sweeper this phase has no reason to build.
**Consequence to log:** the loser's 409 propagates through `run`'s generic `except Exception`, which rolls back and deletes the loser's *own* claim (`release_key`) -- a losing caller's retry re-executes rather than replaying, which is correct (its request never had any effect), but it is the same mechanism the TTL entry below has to coexist with.

## The re-run-idempotency index must be unconditional, not scoped to `resolution = 'unresolved'`

**Chosen:**
```sql
CREATE UNIQUE INDEX uq_recon_findings_open
  ON reconciliation_findings (finding_type, transaction_id, settlement_line_id)
  NULLS NOT DISTINCT;
```
with **no** `WHERE` clause, and findings inserted via `ON CONFLICT (finding_type, transaction_id, settlement_line_id) DO NOTHING RETURNING ...`. The resolver (`ledger.reconciliation.resolver.resolve`) is called with, and only ever processes, that `RETURNING` set -- never the matcher's full in-memory classification.
**Rejected (first draft, caught by design review):** the same index scoped `WHERE resolution = 'unresolved'`.
**Why the scoped version is actively wrong, not just unnecessary:** it excludes exactly the rows that most need suppressing on re-run. A `suppressed` `in_flight` finding, or an `auto_resolved` `unexpected_settlement`, falls outside a `resolution='unresolved'` predicate and would be **re-inserted** by the next run -- and in the `unexpected_settlement` case, re-adjusted, a genuine double-post of real money that `verify_global_balance` cannot catch, because the ledger stays internally balanced, just wrong by 2×. The unconditional index has no such gap: a recurring `missing_settlement`/`in_flight` is silently skipped regardless of its resolution, an operator's manual `suppress` is never undone by a later run (nothing ever removes the row from the index), and an auto-resolved finding can never be adjusted twice.
**Why driving the resolver off `RETURNING`, and not the matcher's classification, is the other half of the same fix:** without it, a conflicting (already-open) finding that the `INSERT` correctly skipped would still reach the resolver from the matcher's in-memory list and get adjusted anyway -- the index alone is not sufficient; the resolver's input has to be the rows that actually got created.
**Not caught by `alembic check`:** reading `alembic/ddl/postgresql.py` confirms `compare_indexes` checks `nulls_not_distinct` but never compares a `postgresql_where` predicate at all -- a scoped predicate could drift silently between the migration and the model. One more reason to prefer the unconditional form: there is no predicate to drift.
**Belt-and-suspenders, not instead of the index:** every match (pass 1, pass 2 regardless of delta, pass 3) sets `settlement_lines.matched_transaction_id`, which removes the line from future *candidate sets* entirely (SPEC.md §7 "set on every match"). An auto-resolved `unexpected_settlement`'s line gets its `matched_transaction_id` re-pointed at the *adjusting* transaction once resolved -- the same trick already used for `duplicate_settlement`'s redundant line(s).
**`findings_by_type` reports two counts, not one:** `observed` (the matcher's full classification) and `created` (the `RETURNING` count). Without the split, a re-run's response would read as a clean ledger while genuinely open drift is still sitting there unresolved.

## Reversals are excluded from the transaction candidate set

**Chosen:** the matcher's transaction candidate set adds `reversal_of IS NULL` to SPEC.md §7's literal `status='posted', source='api'`.
**Why:** a reversal deliberately carries `external_ref = NULL` (Phase 2's decision, made for exactly this reason) and has no settlement of its own -- pass 1 can never match it, and without this exclusion it becomes a spurious `missing_settlement` (or, worse, an accidental pass-3 fuzzy match against an unrelated settled line, since a ref-less reversal is otherwise a perfectly plausible fuzzy candidate). This is the non-obvious half of that Phase 2 decision finally being used.

## Candidate-set ordering is a deterministic total order, not "however Postgres returns rows"

**Chosen:** the transaction candidate set is ordered `(created_at, id)`; the settlement-line candidate set is ordered `(value_date, id)`. Pass 1's "match first, remainder duplicate" and pass 3's greedy nearest-date pairing are both defined only in terms of this order.
**Why:** `created_at`/`value_date` are not unique -- the same non-uniqueness the pagination cursor's `id` tiebreak already documents (`now()` is transaction-start time, so a multi-leg transaction's rows share one `created_at`). Without an explicit tiebreak, which line "wins" pass 1's duplicate-settlement selection, and which candidate wins pass 3's greedy pairing, would depend on incidental physical row order -- making the fault suite's "duplicated lines -> `duplicate_settlement`" row non-deterministic across runs.

## Settlement-line candidates are widened by `RECON_FUZZY_DAYS` on both sides; residue is not

**Chosen:** the line candidate set spans `[window_start - fuzzy_days, window_end + fuzzy_days]` (UTC calendar dates), but a line pulled in *only* by that margin is never itself reported as `unexpected_settlement` -- residue is re-checked against the true `[window_start, window_end]` window.
**Why:** pass 3 needs to reach a line whose `value_date` lands just outside the true window (a transaction posted the day before `window_start` whose settlement lands the day before that). Without widening the candidate set, that transaction is unmatchable and permanently misclassifies as `missing_settlement` on every run. Without the true-window recheck on the *unexpected_settlement* side, every widened line pulled in but left unmatched would be reported as drift it was never actually inside the window for.
**New setting:** `RECON_FUZZY_DAYS` (default 2, matching SPEC.md §7's hardcoded "±2 days") -- pulled into `ledger/config.py` so the widening and pass 3's `abs(...) <= fuzzy_days` check can't drift apart from each other.

## Pass 2 always matches, regardless of delta size or currency

**Chosen:** once a settlement line is selected by pass 2 (ref-only), `matched_transaction_id` is set unconditionally -- the `currency_mismatch`/`amount_mismatch` finding is emitted *in addition to*, not instead of, the match.
**Why:** SPEC.md §7 says "set on every match"; if a large `amount_mismatch` or any `currency_mismatch` didn't count as a match, it would re-enter the candidate set and be re-reported on every future run even though it's already a known, tracked finding (the unconditional index handles the finding-row side of this, but the line would otherwise keep being offered to pass 1/pass 3 too, needlessly). `currency_mismatch` computes no `delta_amount` (left `NULL`) -- a bigint difference between two currencies is meaningless, so the currency check happens strictly before any delta arithmetic.

## `amount_mismatch`'s asset leg: single non-clearing leg, or fall back to the clearing account

**Chosen:** the resolver looks for exactly one non-clearing, asset-type entry on the mismatched transaction. If there isn't exactly one (a transfer between two asset accounts has two; a transaction with no asset leg at all has zero), it falls back to `clearing(currency)` as the counter-leg instead of leaving the finding unresolved.
**Why:** without the fallback, the single most common real-world case a payments ledger has -- an asset-to-asset transfer -- would never auto-resolve, silently defeating the point of having a threshold at all. `is_clearing` already exists for exactly this purpose (see above); reusing it here means `amount_mismatch` and `unexpected_settlement` share one account-selection rule instead of two.
**Sign convention, both adjustment types:** the resolver never assumes a positive amount. `unexpected_settlement`: `line.amount > 0` -> debit clearing / credit suspense; `< 0` -> the reverse. `amount_mismatch`: `delta = line.amount - txn.amount > 0` -> debit asset leg / credit suspense; `< 0` -> the reverse. `settlement_lines.amount` (unlike `entries.amount`) carries no `CHECK (amount > 0)` in SPEC.md §3 -- a real feed contains refunds.

## Every resolver adjustment gets its own `session.begin_nested()`, distinct from `posting.py`'s existing SAVEPOINT

**Chosen:** `ledger.reconciliation.resolver._adjust` wraps the *entire* `post_transaction()` call in its own nested savepoint and catches `LedgerError`, leaving the finding `unresolved` with a logged reason on failure, then continuing the loop.
**Rejected (first draft, caught by design review):** relying on `posting.py`'s existing `SAVEPOINT` (Phase 2, "`SAVEPOINT` around the transaction insert") to absorb a failed adjustment.
**Why the existing savepoint doesn't cover this:** it wraps only the `INSERT INTO transactions` statement, specifically to survive the idempotency-key/`reversal_of` unique-violation backstop. `InsufficientFunds`, `CurrencyMismatch`, `AccountNotFound`, and `InvalidTransactionShape` are all raised at steps 1-5 of `post_transaction`, *before* that savepoint ever opens -- no SQL has failed yet, so there's nothing for it to roll back to. Worse, an `IntegrityError` from the entries insert or the outbox `emit_event` (both *after* the savepoint closes) would poison the whole outer transaction (`25P02`) with no savepoint left to recover from, failing every later finding in the same run's loop. This is the general form of the "one expected failure must not poison the session" pattern applied one level higher, because the resolver -- unlike a single API request -- posts several transactions in a sequence.
**Deterministic idempotency key, defense in depth:** every adjustment posts with `idempotency_key = f"recon-adjust:{finding_id}"` rather than none. The unconditional findings index and the RETURNING-driven resolver are what actually prevent a double-post; this key means that even a future bug that somehow drove the resolver off the wrong row set would still hit the `transactions.idempotency_key` unique constraint before a second adjustment for the same finding could land.

## The reconciliation-run window is read from the DB clock, not Python's

**Chosen:** `execute_run` does `now_ts = (SELECT now())` once, on the run's own connection, and derives `window_end`/`window_start`/`cutoff_at` from it -- and passes `now_ts` explicitly into the `reconciliation_runs` insert, overriding the `started_at` column's `server_default=now()`.
**Why:** this is the Phase 3 "staleness is evaluated with the Postgres clock, not the app clock" decision applied again, for the same reason: `transactions.created_at` and `settlement_lines.ingested_at` are both written by the DB server, so a window computed from the app host's clock could disagree with them under any clock skew -- and would disagree with the run row's own `started_at` if that were left to its server default instead of being pinned to the same `now_ts`.

## The reconciliation run's idempotency TTL is its own, larger setting

**Chosen:** `POST /v1/reconciliation/runs` uses a dedicated `get_reconciliation_idempotent_request` dependency backed by `RECON_RUN_LOCK_TTL_SECONDS` (default 300s), not the shared `IDEMPOTENCY_LOCK_TTL_SECONDS` (30s) every other idempotent route uses.
**Why:** a run over a window with real volume in it can legitimately take longer than 30 seconds. If it did, a concurrent retry with the same key would see the lock as stale, reclaim it (rewriting `locked_at`), fail to acquire the advisory lock, and its `except Exception` handler would call `release_key` -- fenced on `locked_at`, so it deletes the row, because the *reclaimer* now owns the newest `locked_at`. The original run, still executing, would then call `complete_key` (`WHERE status = 'in_progress'`), find no matching row, raise `IdempotencyStateError`, and roll its own otherwise-successful run back for no ledger-level reason at all.
**Accepted limitation:** this is a bound, not a full fix -- a run that blows through even the higher TTL still needs an async execution model. `worker/recon_scheduler.py` stays a stub for Phase 4, calling the same `ledger.reconciliation.runner.execute_run` when it lands, rather than inventing a second code path; running synchronously inside the request is an explicit, accepted Phase 4 choice, not an oversight.

## A failed run's audit row is written outside the run's single commit, on a separate connection

**Chosen:** `verify_global_balance` is called from inside `execute_run`, before the transaction commits (SPEC.md §7: "fail the run loudly if it does not hold"). If it fails, `ReconciliationRunFailed` is raised, which propagates out through `IdempotentRequest.run` and rolls the *entire* transaction back -- correct, because adjustments that would leave invariant 7 broken must never commit. Before re-raising, `execute_run` writes a `status='failed'` `reconciliation_runs` row on a **separate connection** (`ledger/db/engine.py`'s module-level engine, not the request session), carrying the window bounds and the failing per-currency report.
**Why a second write is necessary at all:** without it, the failed run leaves no trace whatsoever -- the very row that would explain what happened rolls back along with everything it's trying to explain. This is the one place in Phase 4 that writes outside the run's single commit.
**Consequence:** `status='running'` is never durably observable by another session (the row exists only inside this transaction until it commits as `'completed'`) -- this is intentional, not a gap. A concurrent run attempt is blocked by the advisory lock, not by any other session reading this column.

## No new outbox event type for reconciliation

**Chosen:** adjusting transactions go through the same `post_transaction()` every other posting does, so they already emit `transaction.posted` into `outbox_events` inside the run's single commit, for free.
**Why:** SPEC.md §8 names no reconciliation-specific event, so none is invented. This also keeps the fault suite's `ledger_row_counts()` outbox assertions meaningful without a Phase 4 special case.

## Reconciliation metrics are structured log events, same as Phase 3's idempotency events

**Chosen:** `reconciliation.run_started` / `.finding` / `.auto_resolved` / `.unresolved` / `.run_completed` / `.run_failed`, carrying `finding_type` where relevant, logged via the existing `structlog` setup rather than wiring up `reconciliation_findings_total{type}` (SPEC.md §9) now.
**Why:** identical reasoning to the Phase 3 entry above -- there is still no `/metrics` endpoint until Phase 7, and these events are named to match the counter they'll eventually back.

## Ingest dedup key adds currency to SPEC.md §7's literal tuple

**Chosen:** `ingest_batch` dedups within a batch on `(external_ref, amount, currency, value_date)`, not the literal `(external_ref, amount, value_date)` SPEC.md §7 writes. A `NULL external_ref` line is never deduped against another (`NULL <> NULL`), including against itself.
**Why:** two same-day, same-amount, same-ref lines are legitimately distinct settlements if they're denominated in different currencies -- ref reuse across currencies happens with real payment processors, and the literal tuple would silently drop one.

## `reconciliation_findings.created_at`, added for pagination

**Chosen:** a `created_at timestamptz NOT NULL server_default now()` column, not in SPEC.md §3's column list for this table, plus `ix_reconciliation_findings_run_id (run_id, created_at, id)`.
**Why:** `GET /v1/reconciliation/runs/{id}/findings` needs to paginate, and every other paginated listing in this project uses the opaque, versioned `(created_at, id)` keyset cursor (`ledger/schemas/pagination.py`) -- findings had no other column that could serve as a stable ordering key.

## Phase 4 breaks the zero-migration streak

**Chosen:** `migrations/versions/0002_reconciliation_indexes.py` -- the first migration since `0001_initial_schema`, after Phase 2 and Phase 3 both shipped zero.
**Why:** unlike Phases 2 and 3, this one isn't optional. `0001` created exactly four indexes (`ix_accounts_currency`, `ix_transactions_created_at`, `ix_transactions_external_ref`, `ix_entries_account_id_created_at`) -- **none** on `settlement_lines` or `reconciliation_findings`, both of which the matcher and the findings API now scan continuously. `accounts.is_clearing` is also a genuinely new column. `alembic upgrade head` -> `downgrade base` -> `upgrade head` -> `alembic check` all pass, confirming the model `__table_args__` and the migration agree exactly.
**Correction (made during Phase 5):** the line above was wrong. `0001_initial_schema.py` already creates `ix_webhook_deliveries_status_next_attempt` on `(status, next_attempt_at)`, matching `WebhookDelivery.__table_args__` exactly -- which is why `alembic check` was green through Phase 4 despite this note. The audit that produced this section was scoped to `settlement_lines`/`reconciliation_findings` and over-generalized to `webhook_deliveries` without checking it. See the Phase 5 section below for what `0003` actually needed.

## `runner.py`, not named in SPEC.md §11's tree

**Chosen:** `ledger/reconciliation/runner.py::execute_run` orchestrates the advisory lock, the run row, `matcher.match` -> the findings insert -> `resolver.resolve` -> `verify_global_balance`, and the `findings_by_type` counts. `ledger/api/routes/reconciliation.py`'s `POST /runs` handler is a thin wrapper calling it through `IdempotentRequest.run`.
**Why:** SPEC.md §11 doesn't name this file, but keeping orchestration out of the route means the future `worker/recon_scheduler.py` (SPEC.md §12 Phase 4 build-order item, still a stub) has one call to make instead of duplicating the route's logic.

## Known, accepted gap: a settlement matched before its transaction is reversed

**Chosen:** left unhandled. A transaction reversed *after* its settlement line already matched leaves a settled line against a now-net-zero ledger effect, and no SPEC.md §7 finding type detects it.
**Why not fixed:** SPEC.md §7 defines exactly six finding types over a matcher/settlement-line model that has no notion of "this settlement's transaction was later reversed." Detecting it would mean either a new finding type not in the spec, or re-scanning every historically-matched line on every run (unbounded cost, growing forever) -- both are decisions bigger than "finish Phase 4 as specified." Recorded here so it isn't silently assumed to be handled.

# Phase 5 — Outbox + Webhooks

## Fan-out tracked by `outbox_events.fanned_out_at`, not a `NOT EXISTS` anti-join

**Chosen:** a nullable `fanned_out_at timestamptz` column plus a partial index `ix_outbox_events_unfanned ON outbox_events (created_at) WHERE fanned_out_at IS NULL`. The dispatcher's fan-out step claims rows from that index with `FOR UPDATE SKIP LOCKED`, inserts one `webhook_deliveries` row per active endpoint via `INSERT ... ON CONFLICT (event_id, endpoint_id) DO NOTHING`, then sets `fanned_out_at = now()` on the claimed events.
**Rejected:** a `NOT EXISTS` anti-join against `webhook_deliveries` with no schema change. It looks free, but the un-fanned set never shrinks when zero endpoints are registered -- which is the state of every integration test, `scripts/seed.py`, and any deployment before the first endpoint is created. Every poll would then re-scan the entire, ever-growing `outbox_events` table to insert zero rows, forever, with no bound.
**Rejected:** a `created_at` high-water mark instead of a column. Postgres `now()` is transaction-start time, so a long-running transaction that started before the watermark but committed after it would become permanently invisible to the dispatcher -- a silently dropped event, the exact failure the outbox pattern exists to prevent.
**Consequence:** an endpoint registered *after* an event was fanned out never receives that event. Fan-out is a one-time snapshot of the active endpoint set at drain time, not a live subscription replayed against history. This is the intended semantic (a new subscriber shouldn't receive a backlog it never asked for), but it means "register an endpoint, then post a transaction" is the only order that reliably delivers.
**Also:** `alembic`'s `compare_indexes` does not compare `postgresql_where` (see the Phase 4 entry on `docs/DECISIONS.md`'s own index-drift caveats), so the partial predicate on `ix_outbox_events_unfanned` could drift from the model silently. `tests/integration/test_migrations.py::test_unfanned_outbox_index_predicate_matches_the_model` pins the actual `pg_indexes.indexdef` directly rather than trusting `alembic check` alone.

## Endpoint secrets are stored in plaintext, unlike `api_keys.key_hash`

**Chosen:** `webhook_endpoints.secret` is stored as the server-generated (`secrets.token_urlsafe(32)`) plaintext value, returned exactly once in the `POST /v1/webhooks/endpoints` response body and never again -- `WebhookEndpointRead` has no `secret` field at all, so no future change to that model can leak it by accident.
**Why this looks like a contradiction but isn't:** an API key (Phase 1's `api_keys.key_hash`) only ever needs to be *compared* against a presented value, so a one-way hash is strictly better -- it protects the credential even if the table leaks. An HMAC webhook secret must be *replayed* on every delivery to compute the signature; a one-way hash would make signing impossible. Hashing it was never on the table.
**Mitigation:** the secret authenticates our outbound request to one endpoint we control the registration of -- it grants no access back into the ledger. It never appears in a response after creation, in a log line (the dispatcher logs delivery outcomes, never the signature or the secret), or in an error body.

## The signed payload is built from bytes, not an f-string over bytes

**Chosen:** `ledger/webhooks/signing.py::signing_payload` builds `f"{timestamp}.".encode("ascii") + raw_body`.
**Rejected:** the literal reading of SPEC.md §8's `f"{timestamp}.{raw_body}"`, evaluated as an actual Python f-string with `raw_body: bytes`. An f-string interpolates `bytes` via its `repr()` (`b'{"id": ...}'`, backslash escapes and all) -- the resulting signature is internally consistent (the dispatcher would both sign and never need to un-sign this string) but matches no receiver implemented against the documented HMAC scheme, because no one else would reasonably interpolate a repr.
**Also:** the dispatcher serializes the envelope exactly once (`serialize_envelope`, sorted keys, compact separators) and sends those same bytes as the request body (`content=body`, explicit `Content-Type` header) -- never `httpx`'s `json=` kwarg, which re-serializes independently and would silently produce a body that doesn't match the signature computed over the first serialization.

## The claim is one `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING id`

**Chosen:** a single statement, executed inside one `engine.begin()` transaction.
**Rejected:** SPEC.md §8's literal two-statement pseudocode (`SELECT ... FOR UPDATE SKIP LOCKED`, then a separate `UPDATE ... WHERE id IN (...)`).
**Why:** the two-statement form leaves a window between the SELECT and the UPDATE in which nothing prevents the same rows from being selected by a concurrent dispatcher (SKIP LOCKED only protects against a second `SELECT ... FOR UPDATE`, not against a second dispatcher's own `SELECT` racing ahead of the first's `UPDATE`). Folding both into one statement closes that window at no extra cost and is still served by `ix_webhook_deliveries_status_next_attempt`.

## The stale-claim sweep does not increment `attempt_count`

**Chosen:** `sweep_stale_claims` moves a `delivering` row whose `claimed_at` is older than `webhook_stale_claim_seconds` back to `pending` with `claimed_at = NULL`, leaving `attempt_count` untouched.
**Why:** a worker killed mid-delivery made no observation of the receiver -- it doesn't know whether the request was ever sent, let alone how the receiver responded. Charging that crash against the delivery's retry budget would shrink the number of *real* receiver-facing attempts for a fault that was entirely on our side. This is also what makes delivery **at-least-once** rather than at-most-once: a redelivered event carries the same `X-Ledgerline-Event-Id`, and a receiver that dedupes on it (documented in `README.md`) sees no observable difference from a single delivery.

## Manual retry (`POST /v1/webhooks/deliveries/{id}/retry`) is `dead`-only and resets the attempt budget

**Chosen:** the route rejects anything but `status = 'dead'` with 409 `/errors/delivery-not-retryable`, then a compare-and-swap `UPDATE ... WHERE id = :id AND status = 'dead'` sets `status = 'pending'`, `next_attempt_at = now()`, `attempt_count = 0`, `claimed_at = NULL` -- the same layered-guard shape (`SELECT ... FOR UPDATE` + CAS `UPDATE`) as `resolve_finding` (Phase 4).
**Why `dead`-only:** a `succeeded` row has nothing to replay; a `pending`/`delivering` row is already queued, and re-queueing it would race the dispatcher's own claim rather than add anything.
**Why reset, not preserve, `attempt_count`:** "manual DLQ replay" means the operator has (presumably) fixed whatever was wrong at the receiver -- leaving `attempt_count` at 8 would let the replay die on its very first failure with no budget left. `last_error`/`last_response_code` are left in place for forensics; only the retry state resets.
**Not idempotent:** unlike SPEC.md §6's idempotent-endpoint list, a second concurrent retry call on the same row gets 409 (the CAS `rowcount != 1` case), not a replayed 200 -- there is no request body to fingerprint and no reason a duplicate manual action should silently succeed twice.

## The mock webhook receiver runs a real `uvicorn` server, not `httpx.ASGITransport`

**Chosen:** `tests/mock_receiver`'s `mock_receiver` fixture starts `uvicorn.Server` on an ephemeral port (`port=0`) in a background `asyncio.Task`, and the fault suite's `Dispatcher` makes genuine outbound HTTP calls to it.
**Rejected:** the `httpx.ASGITransport` pattern every other integration test uses (`app_client`, `fault_client`). It has no underlying socket, so it cannot produce a `ReadTimeout`, a `ConnectError`, or a connection reset -- three of SPEC.md §10's four webhook fault rows. Splitting the fault suite so 400/429/500 go through `ASGITransport` while timeout/reset go through a real socket would mean the two groups exercise different code paths in `Dispatcher._deliver_one`, so the thing actually under test (the exception → delivery-status mapping) would never be validated end-to-end in one place.
**Side effect:** `uvicorn`'s websocket protocol import trips `websockets`' own legacy-module `DeprecationWarning` even though this app never uses websockets, which under this project's `filterwarnings = ["error", ...]` turns into a raised exception at server startup and silently prevents `server.started` from ever becoming `True`. Two scoped ignores (`ignore::DeprecationWarning:uvicorn.*` / `ignore::DeprecationWarning:websockets.*`) were added to `pyproject.toml` in the same commit as the fixture.

## `_deliver_one`'s transport-exception messages fall back to the exception's class name

**Chosen:** `_describe(exc)` returns `str(exc) or type(exc).__name__`.
**Why:** `httpx.ConnectError`/`ReadTimeout` frequently stringify to `''` on some platforms (the underlying OS-level error carries no message) -- storing that directly into `last_error` would leave a dead or retrying delivery with an empty, useless diagnostic field. Found by `tests/faults/test_webhook_retry.py`'s `timeout` case, which asserted `last_error` was truthy and failed against the un-fixed dispatcher.

## Webhook metrics are structured log events, same as Phase 3/4

**Chosen:** `webhook.fanned_out` / `.claimed` / `.delivered` / `.retry_scheduled` / `.dead` / `.stale_claim_swept` / `.manual_retry`, logged via the existing `structlog` setup, named to match SPEC.md §9's eventual `webhook_deliveries_total{status}` / `webhook_dlq_depth` counters.
**Why:** identical reasoning to the Phase 3 and Phase 4 entries above -- there is still no `/metrics` endpoint until Phase 7.

## `httpx` promoted from a dev-only to a runtime dependency

**Chosen:** moved from `[project.optional-dependencies].dev` to `[project].dependencies`.
**Why:** through Phase 4, `httpx` was only ever the *test* client (`app_client`, `fault_client`). Starting Phase 5, `ledger.webhooks.dispatcher` makes real outbound HTTP calls in production, so `ledger` itself now imports `httpx` outside of `tests/` -- leaving it dev-only would make a production `pip install .` (no `[dev]` extra) unable to import the package.

# Phase 6 — Dashboard

## Read models live in `ledger/readmodels/`, not `dashboard/`

**Chosen:** framework-free query modules under `ledger/readmodels/` (`balances.py`, `transactions.py`, `reconciliation.py`, `webhooks.py`), following the same rule as `ledger/core` and `ledger/reconciliation` -- never import `fastapi`, `starlette`, or `jinja2`. `dashboard/` holds only templates, static assets, pure formatters (`dashboard/format.py`), thin route functions (`dashboard/views.py`), and the SSE generator (`dashboard/sse.py`).
**Rejected:** `dashboard/queries.py`, which SPEC.md §11's literal repo tree would suggest (it names no query module at all for the dashboard). Not the first addition beyond that tree -- `ledger/reconciliation/runner.py` (Phase 4) is the precedent for adding a module the spec's tree doesn't name.
**Why:** two independent reasons that happen to agree. First, the layering rule: cross-entry domain logic belongs under `ledger/`, regardless of which UI eventually consumes it. Second, the coverage gate (`[tool.coverage.run] source`) covers `ledger/` but not `dashboard/` by default -- putting the actual SQL under `ledger/` means it's gated from day one, and `dashboard/` stays thin enough that gating it too (see below) costs nothing.
**Contrast with `worker/`:** `worker/webhook_worker.py` is excluded from the coverage gate because it is process-lifecycle plumbing with no decisions in it -- all its logic already lives in `ledger.webhooks.dispatcher`, which is gated. `dashboard/`'s views and SSE generator are comparably thin, which is exactly why extending the gate to include them (below) was cheap rather than risky.

## The coverage gate is extended to `dashboard`, not to `worker` or `scripts`

**Chosen:** `[tool.coverage.run] source = ["ledger", "dashboard"]` and `pytest --cov=ledger --cov=dashboard --cov-fail-under=90` in CI.
**Why:** after the split above, `dashboard/` is a few hundred lines of pure formatters and thin routes, all of which the integration suite already drives end-to-end through `app_client`. Excluding it would leave the gate blind to real, testable work instead of protecting genuinely untestable plumbing. `worker/` (process lifecycle) and `scripts/` (CLI wrappers, `scripts/gen_feed.py`'s own drift logic already moved into `ledger.reconciliation.feed` this phase) stay excluded for the same reason they always were.
**Sequencing:** the gate was widened in its own commit, landed last, after the dashboard code and its tests already existed -- so the real aggregate number was observed (96.32%, well above 90%) before the gate started enforcing it, rather than guessing.

## SSE is periodic-poll, not Postgres LISTEN/NOTIFY

**Chosen:** `dashboard/sse.py::event_stream` re-queries every panel on a timer (`DASHBOARD_SSE_INTERVAL_SECONDS`, default 2s) and emits only the panels whose rendered HTML changed since the last tick.
**Rejected:** `LISTEN`/`NOTIFY`. It would need a new migration adding `pg_notify` triggers on `accounts`, `account_balances`, `transactions`, `reconciliation_findings`, and `webhook_deliveries` -- five-plus trigger objects `alembic check` cannot verify (it already can't compare `postgresql_where` predicates; it has no visibility into triggers at all). A dedicated `asyncpg` listener connection also can't be obtained safely through SQLAlchemy's pool without pinning one physical connection per open browser tab for the tab's entire lifetime, and NOTIFY payloads are not durable -- a poll fallback would still be needed for anything missed while disconnected, at which point LISTEN/NOTIFY adds cost without removing any.
**Trigger condition to revisit:** many concurrent viewers, or a latency requirement tighter than the poll interval can meet.

## The SSE loop opens a fresh session every tick; no shared broadcast queue

**Chosen:** each tick does `async with session_factory() as session: ...`, opening and closing a session (and returning its pooled connection) before sleeping until the next tick.
**Rejected:** a module-level `asyncio.Queue` or listener task shared across requests. `tests/conftest.py` documents exactly this hazard for engines: pytest-asyncio hands each test function its own event loop, so anything created on one test's loop and reused by a later test's loop surfaces as "another operation is in progress" or worse. A shared queue bound to whichever request happened to create it would reproduce that failure mode in production too, across multiple uvicorn workers.
**Consequence:** N open dashboard tabs means N independent polling loops issuing the same handful of indexed queries every 2 seconds -- acceptable at this scale, and the simplest design that is also safe to test.

## `StreamConfig` is injected as a FastAPI dependency, not a query parameter

**Chosen:** `dashboard/views.py::get_stream_config` (and, for the same reason, `get_dashboard_session_factory` and `get_demo_engine`) are ordinary dependency functions, overridden in tests via `app.dependency_overrides` -- the exact mechanism `tests/conftest.py`'s `app_client` already uses for `get_session`.
**Rejected:** a `?max_events=` query parameter to bound the stream for tests.
**Why this is what makes SSE testable at all:** `StreamConfig.max_events` lets a test drain `event_stream` to completion instead of abandoning a live async generator. Under `filterwarnings = ["error"]`, an abandoned generator's eventual `RuntimeWarning`/unraisable `GeneratorExit` surfaces as a failure in an unrelated test's teardown -- and `pyproject.toml` already carries an `ignore:coroutine .* was never awaited:RuntimeWarning` entry (added for Windows/ProactorEventLoop asyncpg teardown) that would *mask* exactly this bug rather than catch it. A query parameter would put the same knob on the public HTTP surface, where a real client could pin it; a dependency keeps it internal to tests.
**Also:** `get_dashboard_session_factory`/`get_demo_engine` exist for a second, independent reason -- see the next entry.

## The dashboard never touches the module-level production `engine` directly

**Chosen:** every dashboard code path that needs an `AsyncEngine` or session factory gets one through a dependency (`get_dashboard_session_factory`, `get_demo_engine`) rather than importing `ledger.db.session.async_session_factory` or `ledger.db.engine.engine` at the top of `dashboard/views.py`.
**Why:** every other route's DB access goes through `get_session`, which `app_client`/`fault_client` override in every test. The SSE route and the demo route are the first dashboard code to touch the database *outside* that dependency -- had they imported the global engine directly, they would have been the first code in the whole test suite to actually exercise it, and `tests/conftest.py`'s own reasoning for why `db_engine` is function-scoped (pytest-asyncio hands each test its own event loop; a stale pooled connection from an earlier test's loop surfaces as "another operation is in progress") would have applied to them for free, in production as much as in tests.
**Caught by:** the SSE integration tests hung when this indirection was still missing, before it was added -- the module-level engine's pooled connections, once created on one test's event loop, could not be reused by the next.

## HTML fragments over the wire, not JSON

**Chosen:** each SSE event's `data:` payload is the fully rendered HTML for that panel (`dashboard/templating.py::render_fragment`), consumed by htmx's `sse-swap` extension.
**Rejected:** a JSON snapshot with client-side rendering.
**Why:** one Jinja partial per panel serves the initial full-page render, the no-SSE fragment-refresh fallback, and every SSE update -- there is exactly one rendering path to keep correct, not two that could drift. `data:` cannot contain a raw newline, so `format_sse` splits multi-line HTML into one `data:` line per source line; `tests/unit/test_sse_format.py` pins this framing directly, since a bug here silently corrupts the stream rather than raising.
**Escaping:** Starlette's `Jinja2Templates` autoescapes by default, which is security-relevant here, not cosmetic -- `webhook_deliveries.last_error` is a truncated response body from a third-party endpoint (`ledger/webhooks/dispatcher.py`) rendered directly into the queue view. `tests/unit/test_dashboard_templates.py::test_balances_fragment_escapes_account_name` pins that a hostile account name renders escaped.

## Vendored htmx, not a CDN

**Chosen:** `dashboard/static/vendor/htmx.min.js` and `htmx-ext-sse.js`, with version and SHA-256 recorded in `dashboard/static/vendor/VENDOR.md`.
**Why:** `docker compose up` and the test suite must work with zero egress; a CDN `<script>` tag is also something an operator console showing ledger balances should not depend on executing from a host this repo doesn't control. Two vendored files keep the project's no-build-step property intact -- no npm, no bundler.
**Cost, accepted:** a manual version-bump process (download, recompute the SHA-256, update `VENDOR.md` in the same commit) instead of a version pin in a lockfile.

## Templates resolve from `Path(__file__)`, never a CWD-relative path

**Chosen:** `dashboard/templating.py` builds `TEMPLATES_DIR`/`STATIC_DIR` from `Path(__file__).resolve().parent`, the same idiom `ledger/api/health.py` already uses for `alembic.ini`.
**Why:** `Dockerfile` installs a wheel (`pip install .`, not `-e`), so a CWD-relative `"dashboard/templates"` would only ever have worked by coincidence of the image also copying the source tree next to the installed package. Hatchling's `packages = [...]` ships a first-party package's whole directory tree (all file types, filtered only by VCS ignores) with no extra `pyproject.toml` configuration -- confirmed by a CI step that builds the wheel and greps its contents for `dashboard/templates/index.html` and `dashboard/static/vendor/htmx.min.js`, since "hatchling just ships it" is an assumption worth pinning rather than trusting silently.

## Retry countdowns and DLQ staleness are computed with the DB clock, inside the query

**Chosen:** `ledger/readmodels/webhooks.py::load_delivery_queue` computes `seconds_until_retry` as `ceil(extract(epoch from next_attempt_at - now()))` in SQL, and `claim_is_stale` from the same `now()` compared against `webhook_stale_claim_seconds`.
**Rejected:** returning the raw `next_attempt_at` and subtracting `datetime.now(UTC)` in Python (or a Jinja filter).
**Why:** the same "DB clock, not the app clock" rule already established for idempotency staleness and reconciliation windows -- and, concretely, it's what makes the countdown assertable against an *exact* integer in a test (`backdate_next_attempt`-style helper, then assert `seconds_until_retry == 30`) instead of a fuzzy range.

## The client-side countdown ticker never reads the browser's clock

**Chosen:** `dashboard/static/ledgerline.js` counts down from the server-computed `data-seconds` value using only `Date.now()` deltas measured entirely on the client, between one SSE render and the next.
**Why "skew-corrected":** the countdown's *absolute* correctness comes entirely from the server-side `seconds_until_retry` computed above; the browser's clock is only ever used to measure elapsed time since that value was rendered, never compared against the server's clock. A workstation with the wrong time of day therefore cannot produce a wrong countdown -- there is no clock comparison to get wrong.
**Consequence for change detection:** an earlier version embedded a live `data-server-time` timestamp in the webhooks fragment for this purpose, which meant the fragment's HTML differed on every single tick (defeating per-panel change detection even when nothing else changed) -- caught by `tests/integration/test_dashboard_sse.py`'s heartbeat-only-on-no-change assertion. Removed; the ticker needs no server timestamp at all, only the per-element `data-seconds` value, since a DOM node freshly swapped in by SSE has no prior countdown state to preserve.

## HTML error containment is a shim in front of the existing RFC 7807 handlers, not a replacement

**Chosen:** `ledger/api/errors.py` exposes its handlers via a public `PROBLEM_HANDLERS` mapping (keyed by exception type). `dashboard/errors.py::install_html_error_handlers` re-registers the same exception types with a wrapper that delegates to the original handler for any request outside `/dashboard`, and renders an HTML page (or, for `HX-Request`, a 200 toast fragment -- htmx does not swap a non-2xx response body at all) for anything under it.
**Why a public mapping instead of reaching into the module-private handler functions:** keeps the dependency arrow one-way (`dashboard -> ledger.api`, never the reverse) explicit and typed, without `dashboard/` importing names that look, and are named, private.
**Pinned:** the existing `tests/unit/test_error_catalog.py` and `tests/integration/test_transactions_errors.py` stay green unmodified -- proof the `/v1` JSON contract is byte-identical to before this phase.
**No new URIs:** Phase 6 introduces no new `error_type` values; the dashboard is read-only HTML, and its own failures render through this containment layer rather than the RFC 7807 catalog.

## The dashboard is mounted into the existing `FastAPI()` app, not a second one

**Chosen:** `create_app()` calls `app.include_router(dashboard_router, prefix="/dashboard", ...)` and `app.mount("/dashboard/static", StaticFiles(...))` on the same app instance, gated behind `settings.dashboard_enabled`.
**Rejected:** a separate `FastAPI()` sub-application mounted at `/dashboard`.
**Why:** `app.dependency_overrides` does not propagate into a mounted sub-application -- a second app would silently break `tests/conftest.py`'s `app_client` fixture's override of `get_session` for every dashboard test, and would need `RequestIdMiddleware` and the RFC 7807 handlers wired up a second time.
**Flag for Phase 7:** `/dashboard/*` sits outside `/v1` and has no client contract to version, which is also why it will need to be explicitly excluded from Phase 7's API-key auth dependency rather than discovered as a gap then.

## Demo scenario logic lives in `dashboard/demo.py`; `scripts/demo.py` is a thin CLI

**Chosen:** `dashboard.demo.run_demo` holds all the logic; `scripts/demo.py` is `argparse` + `asyncio.run` over it, the same shape Phase 5 gave `worker/webhook_worker.py` around `ledger.webhooks.dispatcher`.
**Why:** `scripts/` is in neither `[tool.hatch.build.targets.wheel].packages` nor the `Dockerfile`'s `COPY` list. A dashboard button handler that `import scripts.demo` would work from a source checkout and fail in the container and in the installed wheel -- the two places this app actually runs.
**Moved as part of this:** `scripts/gen_feed.py`'s drift types and `generate_feed` moved to `ledger/reconciliation/feed.py` so `dashboard/demo.py` could reuse them without importing `scripts/` either; `scripts/gen_feed.py` now imports from there, and its own CLI is unaffected. `tests/faults/test_recon_drift.py` and `tests/unit/test_gen_feed.py` were repointed at the new module in the same commit.

## `run_demo` is one transaction, single commit, for the advisory lock to actually hold

**Chosen:** every write in `run_demo` -- accounts, transactions, the backdate `UPDATE`, settlement ingest, endpoint registration, the reconciliation run -- happens inside one uncommitted session transaction, committed exactly once near the end.
**Rejected:** committing after each step (accounts, then transactions, then ingest, ...), which reads naturally as "a driver, like `Dispatcher`, owns its own transaction boundaries."
**Why:** `pg_try_advisory_xact_lock` releases automatically at the *transaction's* end, not the session's. A session that commits partway through ends that transaction -- and its next statement may not even reuse the same pooled connection -- so the lock guarding "only one demo scenario at a time" would evaporate at the very first intermediate commit. `ledger.reconciliation.runner.execute_run` takes the equivalent lock the same way, for the same reason, and `run_demo` calls it *inside* its own transaction rather than letting it manage one itself.
**Scope note:** `Dispatcher`'s "own its own transaction boundaries" pattern is still the right one for `Dispatcher` itself -- it uses a raw `AsyncEngine` with independent `engine.begin()` blocks per step and never needs a lock to span more than one of them. `run_demo`'s lock does need to span everything, which is what makes its shape different.

## The demo backdates `transactions.created_at`, and only that column

**Chosen:** `_post_and_backdate_transactions` posts through `post_transaction` normally, then runs one `UPDATE transactions SET created_at = now() - make_interval(days => :d) WHERE id IN (...)` over most of the batch, leaving a couple of transactions fresh.
**Why backdating is necessary at all:** under the default `RECON_CUTOFF_LAG_HOURS` (24h), a transaction posted moments ago classifies as `in_flight` and is suppressed as drift, not reported -- a demo that only ever posts fresh transactions would show zero findings. Backdating most of them past the cutoff, while leaving a couple fresh, makes both `in_flight` suppression and real drift visible in the same run.
**Why only `transactions`, not `entries`:** the `entries_no_update` trigger makes `entries.created_at` immutable by design (invariant 2), and `ledger.reconciliation.matcher` reads only `Transaction.created_at` for windowing and cutoff classification -- it never looks at `entries.created_at` at all. Backdating `entries` would be both impossible and unnecessary.
**Accepted cosmetic artifact:** after a demo run, a transaction's `entries.created_at` (real posting time) and its own `created_at` (backdated) diverge. This is demo-only and never happens through any `/v1` route.

## `perturb_max_minor` deliberately straddles the auto-resolve threshold

**Chosen:** the demo's `DriftConfig(perturb_max_minor=800, ...)` against the default `RECON_AUTO_RESOLVE_THRESHOLD_MINOR` of 500.
**Why:** a perturbation range entirely inside or entirely outside the threshold would make every `amount_mismatch` finding resolve the same way, which makes for a boring, uninformative demo. Straddling it means a single run reliably produces both `auto_resolved` and `unresolved` findings side by side -- the concrete thing that makes "recovery is visible" (SPEC.md §12 Phase 6's own phrase) actually true on screen. `tests/faults/test_demo_scenario.py` pins both outcomes for a specific seed (`rng_seed=0`, chosen by an offline search, documented inline) rather than asserting on the full histogram, since which seeds produce both outcomes is itself a statistical property of ~20 transactions, not a guaranteed one for every seed.

## `run_demo` is re-runnable but deliberately not idempotent

**Chosen:** each call generates a fresh `run_tag` and appends a new scenario -- new accounts only if missing, but always new transactions, a new settlement batch, new endpoints, and a new reconciliation run.
**Rejected:** making a second click a no-op if a scenario already ran.
**Why:** the button exists to make recovery *visible*. An idempotent second click that changes nothing on screen would read as a broken button, not a safe one. `tests/faults/test_demo_scenario.py::test_run_demo_is_rerunnable_and_appends_rather_than_replaces` pins the run count going 1 -> 2, not staying at 1.
**Guarded differently:** concurrent double-clicks are handled by the advisory lock (previous entries), not by idempotency -- a second *simultaneous* click reports `ran=False` rather than interleaving with the first; a second *sequential* click is a legitimate new scenario.

## The demo route 404s when disabled, rather than not being registered at all

**Chosen:** `POST /dashboard/demo` is always registered; the handler itself checks `get_settings().demo_enabled` and raises `HTTPException(404)` if it's off.
**Rejected:** conditionally calling `app.include_router` for the demo route only when enabled, mirroring how the whole dashboard router is gated behind `dashboard_enabled`.
**Why:** `create_app()` reads settings once, at app-construction time, via an `lru_cache`d `get_settings()` -- conditionally registering a single route inside an already-thin router adds a second place that state has to be kept in sync with the setting, for a route (unlike the whole dashboard) with no wiring cost to always mounting it. 404, not 403: a disabled feature shouldn't reveal that it exists.
**Default:** `demo_enabled` defaults to `False`; `docker-compose.yml` sets `DEMO_ENABLED=true` only for local `app`, never implied by `ENVIRONMENT`. It writes real transactions -- never enable it against a ledger you care about.

## Phase 6 ships zero migrations

**Chosen:** no new Alembic revision. All four dashboard views read existing tables and columns; SSE is stateless; the demo scenario writes only through existing service functions (`post_transaction`, `ingest_batch`, `execute_run`) plus one `UPDATE transactions SET created_at`, which needs no schema change.
**Matches:** the Phase 2 and Phase 3 "ships zero migrations" precedent, elsewhere in this document.
**Deferred, with trigger conditions:** an index on `reconciliation_runs.started_at` (currently a full-table seq scan + sort, free at one row per manual run -- revisit if `worker/recon_scheduler.py` starts producing runs continuously) and a partial index on `webhook_deliveries WHERE status = 'dead'` (revisit past roughly 10^6 delivery rows).

## `jinja2` is a runtime dependency from the start

**Chosen:** `jinja2>=3.1,<4` in `[project].dependencies`, not `[project.optional-dependencies].dev`.
**Why:** identical reasoning to the `httpx` promotion above -- `dashboard/` ships in the wheel and imports `jinja2` outside of `tests/`, so a production `pip install .` (no `[dev]` extra) must be able to import it.

---

# Phase 7 — Deploy

## API key auth: router-level `dependencies=`, not middleware or per-route

**Chosen:** `V1_DEPENDENCIES = [Depends(require_api_key), Depends(enforce_rate_limit)]` (`ledger/api/deps.py`), attached to each `/v1` router's `include_router(..., dependencies=V1_DEPENDENCIES)` call in `ledger/api/main.py`.
**Rejected:** ASGI middleware with a path allowlist; a per-route `Depends(...)` added to all eighteen `/v1` operations individually.
**Why:** the exclusion of `/healthz`, `/readyz`, `/metrics`, and `/dashboard/*` becomes structural -- those routers simply never receive the dependency -- rather than a path allowlist a new route could silently fall outside of, which is exactly what `docs/DECISIONS.md` Phase 6 flagged as a risk to revisit here. Middleware also cannot participate in FastAPI's OpenAPI security-scheme generation; per-route `Depends()` would work but multiplies one line into eighteen for no benefit. `tests/unit/test_openapi.py` turns the resulting split into an enforced invariant instead of a convention to remember.

## One `Unauthenticated` (401) for every auth failure mode

**Chosen:** a missing header, wrong scheme, unknown key hash, and an `active=false` key all raise the same `Unauthenticated` (`/errors/unauthenticated`).
**Rejected:** a distinct "key revoked" 403 for the inactive-key case.
**Why:** a response that distinguishes "this key was never valid" from "this key was valid once" turns the endpoint into an oracle a credential-guessing attacker can use to confirm a hit -- unacceptable for a payments API. One error type for all four cases leaks nothing beyond "not currently usable."

## The API key lookup is cached, with negative caching included

**Chosen:** `ledger.api.auth.ApiKeyCache`, a TTL cache (`api_key_cache_ttl_seconds`, default 30s) from key hash to lookup result, storing `None` for unknown hashes as well as hits. One instance per `FastAPI` app (`app.state.api_key_cache`), never a module-level global.
**Rejected:** no cache (a database round trip on every authenticated request); caching only positive lookups.
**Why:** a DB round trip in front of every request would show up directly in the Locust p99 this same phase is asked to report. Negative caching matters independently: without it, a credential-guessing loop turns every guess into a database query, i.e. the cache would make guessing cheaper to send and more expensive to receive. Per-app instance (not global) is what makes `create_app()` produce a fresh, empty cache in every test -- the same reasoning `RateLimiter` below follows.
**Trade-off, accepted:** revoking a key takes up to `api_key_cache_ttl_seconds` to propagate. Set to `0` for immediate revocation (bypasses the cache entirely, at the cost of a DB read per request).
**No constant-time comparison needed:** the lookup is `WHERE key_hash = :h` against a unique index -- Postgres is comparing hashes, not a caller-supplied secret against a value the app echoes back, so there is no timing side channel exposing anything about the preimage.

## No `auth_enabled` kill switch

**Chosen:** API key auth is unconditional on every `/v1` route; no settings flag disables it.
**Rejected:** an `auth_enabled: bool` setting, defaulting `True`, that tests (and only tests) would flip off.
**Why:** unlike rate limiting (below), disabling auth degrades security, not just availability -- a single environment variable between a payments deployment and total exposure is an unacceptable footgun. It would also mean the ~20 pre-existing integration/fault test files never traverse the shipped auth code path, since they'd run with it off.

## Existing tests keep passing by seeding a real key in the shared client fixtures

**Chosen:** `tests/conftest.py::app_client` and `tests/faults/conftest.py::fault_client` both seed one shared test API key (`tests/support/auth.py::seed_api_key`) and send it as a Bearer token on every request they build.
**Rejected:** `app.dependency_overrides[require_api_key]` returning a fake `AuthenticatedKey`, bypassing the real dependency.
**Why:** the auth blast radius turned out to be two fixtures, not the ~20 files that call them -- every existing integration and fault test needed zero changes and now genuinely exercises real auth end to end, including the cache and the database lookup. A dependency override would have deleted that coverage from the entire suite. Both fixtures depend explicitly on `clean_database` (rather than relying on same-scope autouse ordering) so the seed provably runs after the per-test truncate.

## Rate limiting: in-process token bucket, not Redis

**Chosen:** `ledger.api.ratelimit.RateLimiter`, a per-key token bucket (100 req/s, burst 200) held in a plain dict on `app.state.rate_limiter`, refilled lazily on read.
**Rejected:** a shared store (Redis) for a limit enforced across all app processes/machines.
**Why:** SPEC.md §9 says "in-process" explicitly, and SPEC.md §1 excludes horizontal scaling as a goal.
**Consequence, accepted:** the effective limit is per-*worker process*, not per-deployment -- N uvicorn workers or N machines would give N× the nominal limit. `fly.toml` pins exactly one uvicorn worker (`--workers 1`) per single web machine to keep "100/s burst 200" true for the topology this ships to; this is also what keeps the in-process metric counters (below) correct, so both constraints are satisfied by the same one decision.

## Rate limiting has a kill switch; auth does not

**Chosen:** `rate_limit_enabled: bool = True`, checked dynamically inside `enforce_rate_limit` (same pattern as `dashboard/views.py`'s `demo_enabled` check).
**Why:** disabling the rate limit degrades availability, not security -- a defensible flag, unlike auth's. The Locust load test (below) needs to disable or loosen it for a clean, unthrottled run.

## Metrics: a dedicated `CollectorRegistry`, counters at call sites, gauges from the database at scrape time

**Chosen:** `ledger.observability.metrics.REGISTRY`, a `CollectorRegistry()` instance, not `prometheus_client`'s process-global default. In-process `Counter`/`Histogram` objects for events that only happen inside the API process (transactions posted, entries written, idempotency replays/conflicts, posting latency); `Gauge`s for `webhook_deliveries_total`, `webhook_dlq_depth`, and `reconciliation_findings_total`, refreshed from their tables by `refresh_db_gauges` at scrape time.
**Rejected:** the global default registry; in-process counters for the webhook/reconciliation metrics; a custom `prometheus_client.registry.Collector` for the DB-derived gauges.
**Why a dedicated registry:** keeps `process_*`/`python_gc_*` default collectors out of `/metrics` output, and makes double-registration (which raises) impossible across the many `create_app()` calls the test suite makes in one process -- metrics are module-level, defined once at import.
**Why gauges, not counters, for webhook/reconciliation metrics:** `ledger.webhooks.dispatcher` runs inside `worker/webhook_worker.py`, a separate process with no HTTP server of its own -- an in-process counter there could never be scraped. The same is true of any reconciliation run triggered outside the API process. Reading the table is the only source of truth correct across processes and restarts.
**Why not a custom `Collector`:** `prometheus_client` collectors are synchronous, and refreshing these gauges needs an `await`ed database query -- the route handler awaits `refresh_db_gauges` directly instead.

## Idempotency replays and conflicts are counted at the RFC 7807 handlers, not at `ledger.core.idempotency`'s own log sites

**Chosen:** `IDEMPOTENCY_REPLAYS.inc()` in `ledger/api/errors.py::_idempotent_replay_handler`; `IDEMPOTENCY_CONFLICTS.inc()` in `_ledger_error_handler` when `exc.error_type == "/errors/idempotency-conflict"`.
**Rejected:** incrementing next to `ledger.core.idempotency`'s existing `idempotency.replayed`/`idempotency.duplicate_backstop` log events, the way `transactions_posted_total` is counted next to `posting.py`'s own log event.
**Why:** `ledger.api.idempotent.IdempotentRequest._resolve_duplicate` sometimes *converts* a `DuplicateTransaction` into an `IdempotentReplay` (when the original request has since completed) -- counting at the log sites inside `ledger.core.idempotency` would double-count or miscount that conversion. Counting at the two RFC 7807 handlers instead gives exactly one increment per response the client actually saw, regardless of which internal path produced it.

## `GET /metrics` is deliberately unauthenticated

**Chosen:** mounted with no `V1_DEPENDENCIES`, gated only by a `metrics_enabled` setting.
**Why:** Fly's built-in Prometheus scraper polls over the private network (`6PN`) and cannot send an `Authorization` header. This is the one deliberately-open data surface besides health and the dashboard; request volume and DLQ depth become readable to anything on the private network, which is accepted as the cost of using the platform's built-in scraping rather than standing up a separate metrics-auth mechanism SPEC.md §9 doesn't ask for.

## The idempotency retention sweep runs from a scheduled machine, not a background loop or an HTTP endpoint

**Chosen:** `ledger.core.idempotency.sweep_idempotency_keys` (a plain `DELETE ... WHERE status = 'completed' AND created_at < ...`, interval computed in SQL) plus `ledger.admin.sweep`, a one-shot CLI, run from a Fly scheduled machine (`docs/DEPLOY.md`).
**Rejected:** folding the sweep into `worker/webhook_worker.py`'s 1-second poll loop; an admin HTTP endpoint.
**Why:** a webhook dispatcher sweeping idempotency keys would be a layering smell and would need its own interval bookkeeping bolted onto an unrelated loop. An HTTP endpoint invents API surface SPEC.md §9 doesn't list, and a long-running `DELETE` behind a request timeout is the wrong shape. The one-shot CLI is trivially testable (call the function directly) and needs no scheduling logic of its own to get wrong.
**Index is non-partial:** `ix_idempotency_keys_created_at` covers every row, not just `status = 'completed'` -- every row transitions to `completed` eventually, so a partial index would add write-time maintenance for no read-time benefit, and a future "reap abandoned `in_progress` rows" sweep would want the full index anyway.

## `worker/recon_scheduler.py` stays stubbed

**Chosen:** left as the `raise SystemExit("not implemented yet")` stub it has been since Phase 1.
**Why:** Phase 7's spec line (SPEC.md §12) names API key auth, rate limiting, `/metrics`, OpenAPI descriptions, Fly.io deploy, CD, the load test, and the README diagram -- not a reconciliation scheduler. The one thing that would have justified opening it -- "somewhere to run a periodic job from" -- is served instead by the retention sweep's Fly scheduled machine, which needs no daemon process, no interval bookkeeping, and no tests for scheduling logic. `POST /v1/reconciliation/runs` is already idempotent and advisory-locked, so any external scheduler can already drive periodic reconciliation without this file existing.
**Not an oversight:** recorded here explicitly so a future reader sees a decision, not a gap.

## OpenAPI problem responses: `"model": Problem` alone, not combined with a `"content"` override

**Chosen:** `PROBLEM_RESPONSES` (`ledger/api/openapi.py`) documents each RFC 7807 status via `{"model": Problem, "description": "..."}` only.
**Rejected:** adding `"content": {"application/problem+json": {}}` alongside `"model"` to make the generated schema show the real content-type.
**Why:** FastAPI does not replace the media type when both are given -- it adds a second, empty `application/problem+json` entry next to the `application/json` one it generates from `"model"`, which documents the response *worse* (an empty schema under the correct media type, next to a populated schema under the wrong one) than accepting the understated `application/json` label alone. The mismatch between the documented and actual content-type is noted in each response's `description` instead.

## Fly topology: one web machine, one worker machine, exactly one uvicorn worker

**Chosen:** `fly.toml`'s `[processes]` defines `app` (`uvicorn ... --workers 1`) and `worker` (`python -m worker.webhook_worker`) as separate process groups, each on its own `[[vm]]`.
**Why the second process group is not optional:** without it, `ledger.webhooks.dispatcher` never runs in production and every webhook delivery stays `pending` indefinitely -- the API process alone only ever fans out and enqueues deliveries, per Phase 5.
**Why exactly one uvicorn worker:** both the in-process rate limiter and the in-process metric counters are only correct for a single process (see both entries above) -- `--workers 1` is the one line that keeps both of those decisions true simultaneously for the deployed topology, not two independent constraints that happened to agree.

## `DATABASE_URL` is normalized to the asyncpg driver in `Settings`, not left to the provisioner

**Chosen:** `Settings._normalize_database_url`, a `field_validator(mode="before")`, rewrites a bare `postgres://` or `postgresql://` prefix to `postgresql+asyncpg://`; an already-explicit `+driver` is left untouched.
**Why:** `fly postgres attach` (and most managed-Postgres providers) sets `DATABASE_URL` in libpq form, but both `ledger/db/engine.py` and `migrations/env.py` feed the value straight into `create_async_engine`, which requires the `+asyncpg` driver suffix. Without this normalization, the very first deploy's `release_command` (`alembic upgrade head`) would fail before any traffic shifted -- a failure mode that a `docker-compose`-only development workflow (where `DATABASE_URL` is always written by hand, already in the right form) would never surface.

## CD deploys on `workflow_run`, not `push`, and is a no-op until a secret exists

**Chosen:** `.github/workflows/cd.yml` triggers on the `CI` workflow's `workflow_run: [completed]` event, gated further to `conclusion == 'success'`, `head_branch == 'main'`, and `event == 'push'`; every deploy step itself is additionally gated on `secrets.FLY_API_TOKEN != ''`.
**Rejected:** triggering directly on `push: branches: [main]`, relying on `needs:` to gate on CI (not possible across separate workflow files).
**Why `workflow_run`:** GitHub Actions has no cross-workflow `needs:` -- `workflow_run` is the mechanism for "run this only after that other workflow finished", and checking `conclusion == 'success'` is what actually gates on CI passing rather than merely having run.
**Why pin to `head_sha`:** a `workflow_run` checkout defaults to the default branch's current tip, which can have moved past the exact commit CI validated by the time the CD job starts -- `ref: ${{ github.event.workflow_run.head_sha }}` pins to the validated commit.
**Why `cancel-in-progress: false`:** cancelling a deploy mid-`release_command` (which runs `alembic upgrade head`) could leave a migration half-applied against a live database; a second push queues behind the first rather than pre-empting it.
**Deploy-ready, not deployed:** every step that would actually touch Fly is conditioned on the `FLY_API_TOKEN` secret existing, so this workflow lands as a verified no-op -- see `docs/DEPLOY.md` for the manual `fly launch`/Postgres/secret steps this phase deliberately does not perform.

## The Locust load test is manual, never CI-gated

**Chosen:** `loadtest/locustfile.py`, run by hand against docker-compose or a real deployment; no CI job invokes it.
**Rejected:** a CI job asserting a throughput or p99 threshold on every push.
**Why:** a performance assertion on a shared, variable-capacity GitHub Actions runner is either loose enough to prove nothing or tight enough to fail on unrelated infrastructure noise, not genuine regressions. `loadtest/README.md` records observed numbers with the date and machine they came from instead, the same way this document records design decisions with their reasoning rather than enforcing them as executable rules.
**Load test exercises a shared, contended account pair, not only per-user pairs:** without it, the scenario would never exercise the ordered `FOR UPDATE` locking `ledger.core.posting` is built around, and the reported throughput would be measuring an uncontended, unrepresentative best case.
