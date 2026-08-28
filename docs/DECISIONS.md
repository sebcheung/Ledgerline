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
