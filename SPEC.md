# Ledgerline — Idempotent Payments Ledger & Reconciliation Engine

**Complete build specification.** Implement every component described here. All design decisions are resolved — no open questions, no stubs, no `NotImplementedError`.

---

## 1. System Summary

A double-entry accounting ledger with an HTTP API, correct under adversarial conditions: duplicate requests, concurrent writes to the same account, crashed workers, and an external settlement feed that disagrees with internal state.

Four subsystems:
1. **Ledger core** — double-entry posting with enforced invariants
2. **Idempotency layer** — exactly-once effect for retried requests
3. **Reconciliation engine** — detects and resolves divergence against an external feed
4. **Outbox + webhook dispatcher** — durable at-least-once event delivery

### Non-goals
Real payment rails, multi-currency FX conversion, OAuth, horizontal scaling, mobile client. Do not add these.

---

## 2. Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI + Pydantic v2 |
| DB | PostgreSQL 16 (do not substitute SQLite — row locking semantics matter) |
| ORM | SQLAlchemy 2.0, async |
| Migrations | Alembic |
| Worker | Standalone asyncio process polling the outbox (no Celery/Redis) |
| Tests | pytest, pytest-asyncio, hypothesis, testcontainers |
| Deploy | Docker → Fly.io |
| CI | GitHub Actions |
| Dashboard | Jinja2 + htmx, SSE for live updates |

---

## 3. Data Model

All money is `bigint` in **minor units** (cents). No floats, no `NUMERIC`. All timestamps `timestamptz`, stored UTC.

### `accounts`
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| name | text NOT NULL | |
| type | enum | `asset`, `liability`, `equity`, `revenue`, `expense` |
| currency | char(3) NOT NULL | ISO 4217 |
| allow_negative | bool NOT NULL | |
| is_suspense | bool NOT NULL DEFAULT false | reconciliation adjustments land here |
| created_at | timestamptz NOT NULL | |

Index: `(currency)`, partial unique index on `is_suspense` per currency (one suspense account per currency).

### `transactions`
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| idempotency_key | text UNIQUE NULL | **backstop against double-posting** (see §6) |
| external_ref | text NULL | indexed, used by reconciliation |
| description | text | |
| status | enum | `posted`, `reversed` |
| reversal_of | uuid FK NULL | self-referential |
| source | enum | `api`, `reconciliation` |
| created_at | timestamptz NOT NULL | indexed |

Unique partial index on `reversal_of WHERE reversal_of IS NOT NULL` — a transaction can be reversed at most once.

### `entries`
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| transaction_id | uuid FK NOT NULL | |
| account_id | uuid FK NOT NULL | |
| direction | enum | `debit`, `credit` |
| amount | bigint NOT NULL CHECK (amount > 0) | |
| currency | char(3) NOT NULL | |
| created_at | timestamptz NOT NULL | |

Index: `(account_id, created_at)`.

Append-only. Enforce with a trigger:

```sql
CREATE OR REPLACE FUNCTION reject_entry_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'entries is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entries_no_update BEFORE UPDATE OR DELETE ON entries
  FOR EACH ROW EXECUTE FUNCTION reject_entry_mutation();
```

### `account_balances`
Materialized running balance, updated in the same transaction as entry insertion.

| column | type |
|---|---|
| account_id | uuid PK FK |
| balance | bigint NOT NULL |
| currency | char(3) NOT NULL |
| entry_count | bigint NOT NULL |
| updated_at | timestamptz NOT NULL |

This row is the lock target for concurrent posting (§5).

### `idempotency_keys`
| column | type | notes |
|---|---|---|
| key | text PK | client-supplied |
| endpoint | text NOT NULL | scopes the key to a route |
| request_fingerprint | text NOT NULL | SHA-256 of canonicalized body |
| status | enum | `in_progress`, `completed` |
| response_status | int NULL | |
| response_body | jsonb NULL | |
| locked_at | timestamptz NOT NULL | |
| created_at | timestamptz NOT NULL | |

### `outbox_events`
| column | type |
|---|---|
| id | uuid PK |
| event_type | text NOT NULL |
| payload | jsonb NOT NULL |
| created_at | timestamptz NOT NULL |

### `webhook_endpoints`
`id` uuid PK, `url` text, `secret` text, `active` bool, `created_at`.

### `webhook_deliveries`
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| event_id | uuid FK NOT NULL | |
| endpoint_id | uuid FK NOT NULL | |
| attempt_count | int NOT NULL DEFAULT 0 | |
| next_attempt_at | timestamptz NOT NULL | indexed |
| status | enum | `pending`, `delivering`, `succeeded`, `dead` |
| last_error | text NULL | |
| last_response_code | int NULL | |
| claimed_at | timestamptz NULL | stale-claim reclamation |

Index: `(status, next_attempt_at)`.
Unique: `(event_id, endpoint_id)`.

### `settlement_lines`
| column | type |
|---|---|
| id | uuid PK |
| external_ref | text NULL |
| amount | bigint NOT NULL |
| currency | char(3) NOT NULL |
| value_date | date NOT NULL |
| raw | jsonb NOT NULL |
| batch_id | uuid NOT NULL |
| ingested_at | timestamptz NOT NULL |
| matched_transaction_id | uuid FK NULL |

### `reconciliation_runs`
`id`, `started_at`, `finished_at`, `window_start`, `window_end`, `cutoff_at`, `status` (`running`/`completed`/`failed`), `findings_by_type` jsonb.

### `reconciliation_findings`
| column | type |
|---|---|
| id | uuid PK |
| run_id | uuid FK |
| finding_type | enum |
| transaction_id | uuid FK NULL |
| settlement_line_id | uuid FK NULL |
| delta_amount | bigint NULL |
| detail | jsonb |
| resolution | enum (`unresolved`, `auto_resolved`, `manually_resolved`, `suppressed`) |
| resolving_transaction_id | uuid FK NULL |
| resolved_at | timestamptz NULL |

---

## 4. Invariants

Enforced at runtime in the posting path, and verified by property-based tests.

1. **Balance** — for any transaction, `sum(debits) == sum(credits)` per currency
2. **Immutability** — no `entries` row is ever updated or deleted
3. **Derivability** — `account_balances.balance` equals a recomputation from `entries`
4. **Currency consistency** — every entry's currency matches its account's currency
5. **Sign constraint** — accounts with `allow_negative = false` never go negative
6. **Reversal symmetry** — a reversal is the exact negation of the original; the pair nets to zero
7. **Global balance** — across the whole ledger, total debits equal total credits, per currency

Implement `ledger/core/invariants.py` with:
- `assert_transaction_balanced(entries)` — invariant 1, called pre-commit
- `verify_account_derivability(session, account_id)` — invariant 3, recomputes from scratch
- `verify_global_balance(session)` — invariant 7, exposed at `GET /v1/admin/verify`

---

## 5. Ledger Core — `ledger/core/posting.py`

### Concurrency decision: Read Committed + ordered row locks

Use Postgres default `READ COMMITTED`, with explicit `SELECT ... FOR UPDATE` on the `account_balances` rows involved, **acquired in ascending `account_id` order**. Ordered acquisition makes deadlock impossible between concurrent postings.

Rejected: `SERIALIZABLE` — it produces serialization failures under contention requiring a retry loop, and gives no additional guarantee here since explicit locking already serializes access to the contended rows deterministically.

### Algorithm

```
post_transaction(session, entries: list[EntryRequest], *, idempotency_key,
                 external_ref, description, source) -> Transaction:

  1. Validate shape:
       - at least 2 entries
       - all entries share one currency
       - every amount > 0
       - assert_transaction_balanced(entries)  → raise UnbalancedTransaction

  2. Collect distinct account_ids, sort ascending.

  3. SELECT ab.*, a.allow_negative, a.currency
       FROM account_balances ab JOIN accounts a ON a.id = ab.account_id
      WHERE ab.account_id = ANY(:ids)
      ORDER BY ab.account_id
        FOR UPDATE OF ab

     Missing account → AccountNotFound.
     Any account currency != transaction currency → CurrencyMismatch (invariant 4).

  4. Compute per-account delta:
       asset, expense:              debit +, credit -
       liability, equity, revenue:  debit -, credit +

  5. For each account where allow_negative is false:
       if current_balance + delta < 0 → raise InsufficientFunds (invariant 5)

  6. INSERT transactions row (with idempotency_key — unique constraint is the
     double-post backstop).
     On unique violation of idempotency_key → raise DuplicateTransaction,
     which the idempotency layer converts into a stored-response replay.

  7. INSERT all entries rows.

  8. UPDATE account_balances SET balance = balance + delta,
                                 entry_count = entry_count + n,
                                 updated_at = now()
     for each affected account.

  9. INSERT outbox_events row: event_type='transaction.posted',
     payload = serialized transaction + entries.

 10. Return the Transaction. Caller commits.
```

Steps 3–9 occur inside one DB transaction opened by the caller. Nothing here commits on its own.

### Reversal

```
reverse_transaction(session, transaction_id, *, idempotency_key) -> Transaction:

  1. SELECT the original FOR UPDATE. Not found → TransactionNotFound.
     status == 'reversed' → AlreadyReversed.
  2. Build mirrored entries: every debit becomes a credit and vice versa,
     same accounts, same amounts.
  3. post_transaction(mirrored, source='api', description=f'Reversal of {id}')
     with reversal_of = original.id
  4. UPDATE original SET status='reversed'
  5. Emit outbox event 'transaction.reversed'
```

Invariant 6 holds by construction: the mirrored set is the exact negation, so the pair nets to zero.

### `ledger/core/money.py`

```python
@dataclass(frozen=True)
class Money:
    amount: int      # minor units
    currency: str    # ISO 4217

    def __add__(self, other): ...   # raises CurrencyMismatch if currencies differ
    def __sub__(self, other): ...
    def __neg__(self): ...
    @classmethod
    def from_decimal_string(cls, s: str, currency: str) -> "Money": ...
    def to_decimal_string(self) -> str: ...
```

No arithmetic between differing currencies, ever. No float constructor.

---

## 6. Idempotency — `ledger/core/idempotency.py`

Applies to `POST /v1/transactions`, `POST /v1/transactions/{id}/reverse`, `POST /v1/reconciliation/runs`.

### Fingerprinting
Canonicalize: parse body to JSON, sort keys recursively, drop nulls, serialize with no whitespace, SHA-256 hex. Keys are scoped by `(key, endpoint)` — the same key on a different route is a distinct operation.

### Stale lock threshold
30 seconds, config `IDEMPOTENCY_LOCK_TTL_SECONDS`. Tradeoff: shorter → risk of reclaiming a still-running request; longer → a client blocked behind a crashed request waits longer.

### Double-execution safety

Reclaiming a stale lock *does* risk double execution — the original request may still be mid-flight. The `transactions.idempotency_key` unique constraint is what makes this safe: if the original commits first, the reclaiming request's INSERT fails on the unique violation, it rolls back and replays the stored response. The lock is an optimization; the unique constraint is the guarantee.

### Algorithm

```
handle(request, endpoint, key, execute_fn):

  fingerprint = canonical_hash(request.body)

  # Attempt claim
  INSERT INTO idempotency_keys (key, endpoint, request_fingerprint,
                                status, locked_at, created_at)
  VALUES (...,'in_progress', now(), now())
  ON CONFLICT (key) DO NOTHING
  RETURNING key

  if inserted:
      goto EXECUTE

  # Conflict — inspect existing row
  row = SELECT * FROM idempotency_keys WHERE key = :key FOR UPDATE

  if row.endpoint != endpoint:
      → 422 IdempotencyKeyScopeConflict

  if row.status == 'completed':
      if row.request_fingerprint == fingerprint:
          → replay row.response_status / row.response_body   (add header
            Idempotent-Replay: true)
      else:
          → 422 IdempotencyKeyReuse

  if row.status == 'in_progress':
      if now() - row.locked_at < LOCK_TTL:
          → 409 IdempotencyConflict (Retry-After: 1)
      else:
          if row.request_fingerprint != fingerprint:
              → 422 IdempotencyKeyReuse
          UPDATE idempotency_keys SET locked_at = now() WHERE key = :key
          goto EXECUTE

EXECUTE:
  BEGIN
    try:
        result, status = execute_fn(session)
    except DuplicateTransaction:
        # Backstop fired: the original request won the race and committed.
        ROLLBACK
        row = SELECT * FROM idempotency_keys WHERE key = :key
        if row.status == 'completed': → replay row's stored response
        else: → 409 IdempotencyConflict
    UPDATE idempotency_keys
       SET status='completed', response_status=:status, response_body=:result
     WHERE key = :key
  COMMIT        # key completion and ledger write commit atomically
```

The single commit at the end is the crux. A crash before it leaves the key `in_progress` with no ledger write — the retry reclaims after TTL and executes cleanly.

---

## 7. Reconciliation — `ledger/reconciliation/`

### Window and cutoff decisions
- **Window**: rolling 7 days back from run start. Config `RECON_WINDOW_DAYS`.
- **Cutoff lag**: 24 hours. Config `RECON_CUTOFF_LAG_HOURS`. `cutoff_at = run_start - cutoff_lag`.
- Any transaction with `created_at > cutoff_at` is classified `in_flight` and suppressed — the external feed has not had time to report it. Without this rule every run floods with false `missing_settlement` findings.

### Auto-resolution policy
Auto-resolve when `abs(delta_amount) <= RECON_AUTO_RESOLVE_THRESHOLD_MINOR` (default `500`, i.e. $5.00). Above threshold → finding stays `unresolved` and awaits manual resolution via the API. Rationale: bounded blast radius for automated money movement.

### `matcher.py`

```
match(session, run) -> MatchResult:

  txns  = transactions posted in [window_start, window_end], status='posted',
          source='api', not already matched
  lines = settlement_lines with value_date in window, matched_transaction_id IS NULL

  # Pass 1 — exact: external_ref + amount + currency
  build dict keyed (external_ref, amount, currency) from lines
  for each txn with external_ref:
      candidates = bucket.get(key)
      if exactly 1   → match
      if more than 1 → match first, flag remainder duplicate_settlement
      if 0           → defer to pass 2

  # Pass 2 — ref-only (catches amount mismatch)
  for unmatched txns with external_ref:
      lines with same external_ref, any amount
        → match, record delta = line.amount - txn.amount
          finding: amount_mismatch (or currency_mismatch if currency differs)

  # Pass 3 — fuzzy: amount + currency + date window ±2 days
  only for txns and lines that BOTH lack external_ref
  greedy nearest-date pairing; ambiguous (2+ equidistant candidates) → leave
  unmatched rather than guess

  # Residue
  unmatched txn  → in_flight if created_at > cutoff_at, else missing_settlement
  unmatched line → unexpected_settlement
```

Transaction amount for matching = sum of debits (equals sum of credits by invariant 1).

Set `settlement_lines.matched_transaction_id` on every match so runs are idempotent — a re-run over the same window does not re-report resolved items.

### `resolver.py`

| finding_type | resolution |
|---|---|
| `in_flight` | mark `suppressed`, no action |
| `missing_settlement` | leave `unresolved` — the ledger is authoritative; the feed may simply be late beyond the lag |
| `unexpected_settlement` | post an adjusting transaction: debit the settlement's implied asset account, credit suspense (or reverse per sign), amount = line.amount |
| `amount_mismatch` | post an adjusting transaction for `delta_amount` between the transaction's asset account and suspense |
| `currency_mismatch` | leave `unresolved`, always manual |
| `duplicate_settlement` | mark the redundant line resolved with no ledger effect; record in `detail` |

Every auto-resolution is a normal ledger transaction with `source='reconciliation'`, posted through `post_transaction()` so all seven invariants still apply. Never mutate existing entries. Set `resolving_transaction_id` on the finding.

After a run completes, call `verify_global_balance()` and fail the run loudly if it does not hold.

### `ingest.py`
`POST /v1/settlements/ingest` accepts a batch, assigns a `batch_id`, deduplicates on `(external_ref, amount, value_date)` within the batch, stores the original payload in `raw`.

---

## 8. Outbox + Webhooks — `ledger/webhooks/`

### Why an outbox
The event row is written in the same DB transaction as the ledger write. If the ledger commits, the event exists. No dual-write gap.

### `dispatcher.py`

```
loop every POLL_INTERVAL (1s):

  BEGIN
    rows = SELECT * FROM webhook_deliveries
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT BATCH_SIZE
            FOR UPDATE SKIP LOCKED
    UPDATE those rows SET status='delivering', claimed_at=now()
  COMMIT

  for each row (concurrently, bounded by semaphore of 10):
      POST endpoint.url
        headers:
          X-Ledgerline-Event-Id:   {event.id}
          X-Ledgerline-Timestamp:  {unix_ts}
          X-Ledgerline-Signature:  sha256={hmac}
        timeout: 5s connect, 5s read
      2xx        → status='succeeded'
      4xx (non-429) → status='dead' immediately (client error won't fix itself)
      429/5xx/timeout/connection error → schedule_retry(row)

schedule_retry(row):
  attempt = row.attempt_count + 1
  if attempt >= MAX_ATTEMPTS (8):
      status='dead'
  else:
      base  = min(BASE_DELAY * 2 ** (attempt - 1), MAX_DELAY)   # 1s base, 1h cap
      delay = random.uniform(0, base)                            # full jitter
      status='pending', next_attempt_at = now() + delay,
      attempt_count = attempt, last_error, last_response_code
```

Full jitter (not equal jitter) — prevents thundering herd when many deliveries fail simultaneously against one downed endpoint.

### Stale claim reclamation
A separate sweep marks rows `delivering` with `claimed_at` older than 60s back to `pending`. Covers worker crashes mid-delivery. This makes delivery **at-least-once**; the event ID header lets receivers dedupe. Document this in the README.

### `signing.py`
`HMAC-SHA256(secret, f"{timestamp}.{raw_body}")`, hex. Constant-time comparison helper for the mock receiver.

### Fan-out
When an outbox event is created, the dispatcher's fan-out step inserts one `webhook_deliveries` row per active endpoint. Unique `(event_id, endpoint_id)` makes fan-out idempotent.

---

## 9. API Surface

```
POST   /v1/accounts
GET    /v1/accounts/{id}                    includes balance
GET    /v1/accounts/{id}/entries            cursor-paginated

POST   /v1/transactions                     [idempotent]
GET    /v1/transactions/{id}
GET    /v1/transactions                     filter: external_ref, date range, status
POST   /v1/transactions/{id}/reverse        [idempotent]

POST   /v1/settlements/ingest
GET    /v1/settlements                      filter: batch_id, matched status

POST   /v1/reconciliation/runs              [idempotent]
GET    /v1/reconciliation/runs/{id}
GET    /v1/reconciliation/runs/{id}/findings
POST   /v1/reconciliation/findings/{id}/resolve

POST   /v1/webhooks/endpoints
GET    /v1/webhooks/endpoints
GET    /v1/webhooks/deliveries              filter: status, endpoint_id
POST   /v1/webhooks/deliveries/{id}/retry   manual DLQ replay

GET    /v1/admin/verify                     runs invariants 3 and 7
GET    /healthz                             liveness
GET    /readyz                              DB connectivity + Alembic head check
GET    /metrics                             Prometheus text format
```

**Auth**: `Authorization: Bearer <key>`. Keys stored as SHA-256 hashes in an `api_keys` table (`id`, `key_hash`, `name`, `active`, `created_at`). Per-key token bucket rate limit, 100 req/s burst 200, in-process.

**Errors**: RFC 7807 `application/problem+json` with stable `type` URIs:

| type | status |
|---|---|
| `/errors/unbalanced-transaction` | 422 |
| `/errors/insufficient-funds` | 422 |
| `/errors/currency-mismatch` | 422 |
| `/errors/account-not-found` | 404 |
| `/errors/idempotency-key-reuse` | 422 |
| `/errors/idempotency-conflict` | 409 |
| `/errors/already-reversed` | 409 |
| `/errors/rate-limited` | 429 |

**Metrics**: `transactions_posted_total`, `entries_written_total`, `idempotency_replays_total`, `idempotency_conflicts_total`, `reconciliation_findings_total{type}`, `webhook_deliveries_total{status}`, `webhook_dlq_depth`, `posting_latency_seconds` histogram.

---

## 10. Testing

### Unit
Money arithmetic, canonical fingerprinting, backoff computation, HMAC signing, per-account-type delta signs.

### Property-based (hypothesis)
Generate arbitrary valid transaction sequences against generated account sets; after each, assert invariants 1, 3, 4, 5, 7. Include reversals in the generated operation space to cover invariant 6.

### Integration (testcontainers Postgres)
Full request → DB → response paths for every endpoint.

### Concurrency
- 50 threads posting against one account simultaneously → final balance exactly correct, entry count exact
- Two transactions touching the same two accounts in opposite order → no deadlock (validates ordered locking)

### Fault injection — `tests/faults/`
| fault | expected behavior |
|---|---|
| Same key + body, 20 concurrent | 1 execution, 19 replays or 409s; exactly one transaction row |
| Same key, different body | 422 `idempotency-key-reuse` |
| Crash between ledger write and key completion | No transaction row; retry after TTL succeeds |
| Stale lock reclaimed while original in flight | Unique constraint fires; exactly one transaction |
| Webhook receiver: timeout / 500 / connection reset / 429 | Retry with backoff; success once receiver recovers |
| Webhook receiver: 400 | Immediately `dead`, no retries |
| Worker killed mid-delivery | Stale claim swept back to `pending`, redelivered |
| Feed with dropped lines | `missing_settlement` findings |
| Feed with duplicated lines | `duplicate_settlement`, no double ledger effect |
| Feed with perturbed amounts | `amount_mismatch`; auto-resolves if ≤ threshold |
| Transactions posted after cutoff | `in_flight`, suppressed, not reported as drift |
| Reconciliation run twice over same window | Second run produces no new findings |

Mock webhook receiver: FastAPI app with a configurable failure-mode endpoint.

---

## 11. Repo Structure

```
ledgerline/
├── ledger/
│   ├── api/
│   │   ├── routes/       accounts, transactions, settlements,
│   │   │                 reconciliation, webhooks, admin
│   │   ├── deps.py       session, auth, rate limit
│   │   ├── errors.py     RFC 7807 handlers
│   │   └── idempotent.py FastAPI dependency wrapping §6
│   ├── core/
│   │   ├── posting.py
│   │   ├── idempotency.py
│   │   ├── invariants.py
│   │   └── money.py
│   ├── reconciliation/
│   │   ├── matcher.py
│   │   ├── resolver.py
│   │   └── ingest.py
│   ├── webhooks/
│   │   ├── dispatcher.py
│   │   ├── outbox.py
│   │   └── signing.py
│   ├── models/           SQLAlchemy
│   ├── schemas/          Pydantic
│   ├── db/               engine, session
│   ├── observability/    structured logging, metrics
│   └── config.py         pydantic-settings
├── worker/
│   ├── webhook_worker.py
│   └── recon_scheduler.py
├── dashboard/
│   ├── templates/
│   ├── static/
│   └── sse.py
├── tests/
│   ├── unit/ integration/ property/ faults/
│   ├── mock_receiver/
│   └── conftest.py
├── migrations/
├── scripts/
│   ├── seed.py           accounts + realistic transaction history
│   ├── gen_feed.py       settlement feed with injectable drift
│   └── demo.py           end-to-end scenario driver
├── docs/
│   ├── ARCHITECTURE.md
│   └── DECISIONS.md
├── docker-compose.yml
├── Dockerfile
├── fly.toml
└── .github/workflows/ci.yml
```

---

## 12. Build Order

Each phase ends with tests passing and migrations applied.

**Phase 1 — Foundation**
Repo scaffold, `pyproject.toml`, docker-compose (app + Postgres), config via pydantic-settings, SQLAlchemy models for all tables, Alembic initial migration including the append-only trigger, structured JSON logging, `/healthz` + `/readyz`, CI running ruff + mypy --strict + pytest.

**Phase 2 — Ledger core**
`money.py`, `invariants.py`, `posting.py` with ordered `FOR UPDATE` locking. Accounts and transactions endpoints. Reversal. RFC 7807 error handling. Property tests for invariants 1–7. Concurrency tests (50-thread contention, deadlock-order test).

**Phase 3 — Idempotency**
`idempotency.py`, the FastAPI dependency, `transactions.idempotency_key` unique constraint. Full fault suite for duplicates, reuse, concurrent duplicates, crash-mid-transaction, stale-lock reclamation.

**Phase 4 — Settlements + reconciliation**
Ingest endpoint. `matcher.py` three-pass algorithm. `resolver.py` with the auto-resolve threshold. `gen_feed.py` producing feeds with configurable drift. Findings API. Re-run idempotency. Post-run global balance verification.

**Phase 5 — Outbox + webhooks**
Outbox writes inside ledger transactions. Fan-out. `dispatcher.py` with SKIP LOCKED, full-jitter backoff, DLQ, stale-claim sweep. HMAC signing. Mock receiver. Manual retry endpoint. Full webhook fault suite.

**Phase 6 — Dashboard**
Live views: account balances, recent transactions, reconciliation run history and findings, webhook delivery queue with retry countdowns and DLQ depth. SSE endpoint pushing updates. `demo.py` button that seeds a scenario with drift and webhook failures so recovery is visible.

**Phase 7 — Deploy**
API key auth, rate limiting, `/metrics`, OpenAPI descriptions, Fly.io deploy with managed Postgres, migrations on release, GitHub Actions CD on merge to main, Locust load test capturing throughput and p99 posting latency, README with architecture diagram.

**Phase 8 — Reliability**
Recovery metrics (stale-claim sweeps, delivery attempt outcomes and latency, outbox lag, DB error counts, metrics-refresh failures), Prometheus alert rules for every failure mode those metrics can detect, validated in CI with `promtool check`/`test rules` against a Docker-pinned Prometheus without deploying a live Prometheus/Alertmanager anywhere, `docs/RUNBOOK.md` covering detection/remediation/verification per alert plus rollback and migration-failure procedures, a chaos test suite proving crash recovery for both webhook worker crashes/cancellation mid-delivery and real Postgres backend loss (via `pg_terminate_backend`, not monkeypatched exceptions) mid-posting and mid-idempotent-claim, and `docs/CHAOS.md` recording the recovery numbers actually measured from both the automated suite and a live `docker compose` crash-and-restart run.

---

## 13. Conventions

- Money is `bigint` minor units everywhere; `Money` refuses cross-currency arithmetic
- All timestamps `timestamptz`, UTC
- Structured JSON logs carrying `request_id`, plus `idempotency_key` and `transaction_id` where applicable
- `mypy --strict` and `ruff` clean in CI
- Async throughout; no sync DB calls in request paths
- Service functions accept a `session` and never commit — the caller owns the transaction boundary
- `docs/DECISIONS.md` records each non-obvious design choice: what was chosen, what was rejected, why
