# Runbook

Operational response for each `ops/alerts.yml` alert, plus incident procedures
`docs/DEPLOY.md` doesn't cover. See `docs/DEPLOY.md` for first-time
provisioning (`fly launch`, Postgres attach, the first API key, the retention
sweep machine, CD secret) -- this document assumes all of that already exists
and something has gone wrong.

Every alert's `annotations.runbook_url` in `ops/alerts.yml` points at one of
the `##` headings below (`docs/RUNBOOK.md#<alert-name-lowercased>`) -- keep
heading text free of spaces/punctuation so the anchor stays exactly the alert
name, lowercased.

## LedgerlineWorkerStalled

**What fired:** `outbox_lag_seconds` (the age of the oldest un-fanned-out
outbox event) has been above 300s for 5+ minutes. The webhook worker
(`worker/webhook_worker.py`) has no Fly health check of its own (unlike the
`app` process's `/readyz`), so this Gauge -- refreshed from the database at
every `GET /metrics` scrape on the `app` process -- is the primary
cross-process signal that it is still making progress. A steadily growing
lag means it crashed, deadlocked, or is stuck, even though nothing
HTTP-visible has failed. (The worker's own `webhook_stale_claims_swept_total`/
`webhook_delivery_attempts_total`/`webhook_delivery_latency_seconds`, scraped
separately from the worker's own `:9090/metrics` per `fly.toml`'s second
`[[metrics]]` block, are the detailed follow-up once this alert says
something is wrong.)

**Confirm:**

```sh
fly status --app ledgerline
fly logs --app ledgerline --process worker
curl -s https://<app>.fly.dev/metrics | grep outbox_lag_seconds
```

If `fly status` shows the `worker` machine not `started`, or the logs stop
mid-cycle with no further `webhook.fanned_out`/`webhook.claimed` lines, the
process is genuinely dead rather than merely slow.

**Remediate:**

```sh
fly machine list --app ledgerline   # find the worker machine's ID
fly machine restart <worker-machine-id> --app ledgerline
```

If the machine won't come back cleanly, `fly machine destroy <id> --force`
followed by `fly machine run . --app ledgerline` recreates it from the
current image with the same process-group configuration in `fly.toml`.

**Verify recovery:** `outbox_lag_seconds` trending back toward 0 on
`/metrics`, and fresh `webhook.fanned_out` / `webhook.claimed` log lines. The
alert clears once the Gauge drops under 300s for the full 5-minute window.

## LedgerlineDLQGrowing

**What fired:** `webhook_dlq_depth` grew over the last 15 minutes
(`delta(...)`, not `increase()`, since this is a Gauge that can also shrink
when deliveries are replayed). Dead-lettered deliveries never retry
themselves -- something (a receiver outage, a bad endpoint URL, a signing
secret rotated without updating the endpoint) is causing deliveries to
exhaust their retry budget (`webhook_max_attempts`, default 8) and needs a
human to inspect and decide whether to replay.

**Confirm:**

```sh
curl -s https://<app>.fly.dev/v1/webhooks/deliveries?status=dead \
  -H "Authorization: Bearer <key>"
```

or directly against the database (`fly postgres connect --app ledgerline-db`,
or `psql "$DATABASE_URL"`):

```sql
SELECT id, endpoint_id, last_response_code, last_error, attempt_count, created_at
FROM webhook_deliveries
WHERE status = 'dead'
ORDER BY created_at DESC
LIMIT 20;
```

Look at `last_error`/`last_response_code` across the dead rows: a shared
`endpoint_id` or a consistent connection error points at one receiver being
down; a mix of different receivers with `HTTP 4xx` points at something the
sender (us) is doing wrong (a rotated secret, a malformed payload).

**Remediate:** once the underlying cause is fixed on the receiver's end (or
confirmed to be transient), replay each dead delivery:

```sh
curl -s -X POST https://<app>.fly.dev/v1/webhooks/deliveries/<delivery-id>/retry \
  -H "Authorization: Bearer <key>"
```

This is `dead`-only (409 if the row isn't currently `dead`) and resets
`attempt_count` to 0 -- a fresh retry budget, not a continuation of the
exhausted one. `last_error`/`last_response_code` are preserved for
forensics. There is no bulk-replay endpoint; script the loop over the
dead-list response if there are many.

If the receiver is never coming back (an endpoint that's been decommissioned),
deactivate it instead of replaying forever:

```sql
UPDATE webhook_endpoints SET active = false WHERE id = '<endpoint-id>';
```

**Verify recovery:** `webhook_dlq_depth` on `/metrics` stops growing and
drops as replays succeed; `GET /v1/webhooks/deliveries?status=dead` returns
fewer rows.

## LedgerlineStaleClaimStorm

**What fired:** `webhook_stale_claims_swept_total` has been increasing at a
sustained non-zero rate for 15+ minutes. `Dispatcher.sweep_stale_claims`
reclaims rows a crashed worker left in `delivering` -- this should happen
rarely (an occasional restart mid-delivery). A sustained rate means the
worker is crash-looping mid-delivery rather than recovering from an isolated
crash: each reclaim also increments the row's `reclaim_count`, and once that
exceeds `webhook_max_reclaims` (default 8) the row is dead-lettered instead
of retried forever.

**Confirm:**

```sh
fly logs --app ledgerline --process worker
curl -s http://<worker-machine>.vm.ledgerline.internal:9090/metrics \
  | grep -E 'webhook_stale_claims_swept_total|webhook_delivery_attempts_total'
```

(The worker's own `:9090/metrics` -- a second, independent scrape target
from the `app` process's `:8000/metrics`, since these Counters live in the
`worker` process's memory; see `docs/DECISIONS.md`'s Phase 8 entry. Reachable
over Fly's private network, e.g. from another machine in the same app or via
`fly ssh console`.)

Look for repeated `webhook.stale_claim_swept` or `webhook.reclaim_exhausted`
log lines, and check whether the process is actually restarting (an OOM
kill, a crash-looping deploy, a bad image) rather than just slow:

```sh
fly status --app ledgerline
```

```sql
SELECT id, reclaim_count, attempt_count, claimed_at, last_error
FROM webhook_deliveries
WHERE reclaim_count > 0
ORDER BY reclaim_count DESC
LIMIT 20;
```

A cluster of rows all with high `reclaim_count` and recent `claimed_at`
confirms an active crash loop, not historical noise.

**Remediate:** find and fix why the worker keeps dying -- check
`fly logs` for the actual crash (an unhandled exception, an OOM, a bad
deploy) rather than restarting blindly. If a bad release is the cause, see
"Rollback procedure" below. If it's resource exhaustion, check
`fly.toml`'s `[[vm]]` sizing for the `worker` process group.

**Verify recovery:** `webhook_stale_claims_swept_total`'s rate returns to
near-zero and stays there for the alert's 15-minute window; no new rows
accumulating `reclaim_count`.

## LedgerlineDeliveryFailureRatio

**What fired:** over 50% of webhook delivery attempts in the last 10 minutes
ended in `retried` (`webhook_delivery_attempts_total{outcome="retried"}`
versus the total), sustained for 10+ minutes. This is a receiver-side
problem, not ours -- outbound delivery is failing en masse against one or
more endpoints.

**Confirm:**

```sql
SELECT endpoint_id, count(*), last_response_code, last_error
FROM webhook_deliveries
WHERE status IN ('pending', 'dead') AND attempt_count > 0
GROUP BY endpoint_id, last_response_code, last_error
ORDER BY count(*) DESC;
```

If one `endpoint_id` dominates, it's that receiver; if it's spread across
every registered endpoint, suspect something on our side (a bad payload
change, `webhooks/signing.py` producing an unverifiable signature after an
unrelated change, DNS/egress trouble from the Fly worker machine itself).

**Remediate:** if it's receiver-side, there's nothing to do but wait for
their outage to clear -- the full-jitter backoff already spreads retries
rather than hammering a downed endpoint. If it's a shared cause (a bad
deploy that broke signing, for instance), see "Rollback procedure" below.
Once the receiver recovers, deliveries that haven't yet exhausted
`webhook_max_attempts` will succeed on their own; anything that already hit
`dead` needs the `LedgerlineDLQGrowing` replay procedure above.

**Verify recovery:** the ratio in `sum(rate(webhook_delivery_attempts_total
{outcome="retried"}[10m])) / sum(rate(webhook_delivery_attempts_total[10m]))`
drops back under 0.5 and stays there.

## LedgerlinePostingLatencyHigh

**What fired:** p99 `posting_latency_seconds` (time inside
`ledger.core.posting.post_transaction`) has been above 1s for 10+ minutes --
a 4x regression against the Phase 7 load test's observed baseline of p99
under 250ms (`loadtest/README.md`).

**Confirm:**

```sh
curl -s https://<app>.fly.dev/metrics | grep posting_latency_seconds
fly logs --app ledgerline --process app
```

```sql
SELECT count(*), max(now() - created_at) AS oldest_lock_wait
FROM pg_locks
WHERE relation = 'account_balances'::regclass AND NOT granted;
```

A large `pg_locks` wait queue on `account_balances` points at genuine lock
contention (e.g. an unusually hot shared account pair, matching the load
test's own contended-pair scenario); no lock queue but still-high latency
points at the database itself being slow (CPU/IO-starved, autovacuum
running, a missing index after a schema change).

**Remediate:** if it's contention on a specific account pair, there is no
code-level fix within this phase's scope -- SPEC.md §1 excludes horizontal
scaling, and the ordered-locking design is what prevents deadlock, not
throughput limits. If it's database resource pressure, check the managed
Postgres's own metrics/plan size (`fly postgres connect` and standard
Postgres diagnostics: `pg_stat_activity`, `pg_stat_user_tables` for
autovacuum activity).

**Verify recovery:** p99 `posting_latency_seconds` back under 1s (ideally
back near the ~250ms baseline) for the full 10-minute sustained window.

## LedgerlineMetricsStale

**What fired:** `refresh_db_gauges` (`ledger/observability/metrics.py`) has
failed at least once in the last 5 minutes. It swallows its own DB errors
specifically so a database outage can't also take `GET /metrics` down with a
500 -- but that means `webhook_deliveries_total`, `webhook_dlq_depth`,
`reconciliation_findings_total`, and `outbox_lag_seconds` are all silently
stale (frozen at their last successfully-scraped values) while this counter
is incrementing. None of those gauges' own alerts can detect this on their
own -- a stale gauge just looks quiet.

**Confirm:**

```sh
curl -s https://<app>.fly.dev/metrics | grep metrics_db_refresh_failures_total
fly logs --app ledgerline --process app
```

Look for `metrics_db_refresh_failed` log lines (logged with `exc_info=True`)
to see the underlying database error.

**Remediate:** this is a symptom of a database-connectivity problem, not
something to fix in the metrics code -- treat it as a database incident.
Check `fly postgres connect` for reachability, connection limits
(`pg_stat_activity` count against the managed Postgres's max connections),
and whether `/readyz` is also failing (a broader DB-reachability check, not
just this one gauge refresh).

**Verify recovery:** `metrics_db_refresh_failures_total` stops incrementing,
and `webhook_dlq_depth`/`outbox_lag_seconds`/etc. resume moving again on
successive scrapes (a value that hasn't changed across many scrapes while
this counter was climbing was stale, not stable).

## LedgerlineIdempotencyConflictSpike

**What fired:** `idempotency_conflicts_total` (incremented in
`ledger/api/errors.py` whenever a request hits an in-flight idempotency key
lock) has been elevated for 10+ minutes. A low background rate is expected
(legitimate concurrent retries racing each other); a sustained higher rate
suggests either a client retry storm (a caller retrying far too
aggressively) or a lock that isn't being released promptly.

**Confirm:**

```sql
SELECT key, status, locked_at, created_at
FROM idempotency_keys
WHERE status = 'in_progress'
ORDER BY locked_at ASC
LIMIT 20;
```

Rows with `locked_at` far in the past (older than `idempotency_lock_ttl_seconds`,
default 30s) that are still `in_progress` indicate requests that are
genuinely stuck, not just legitimately slow; a healthy system should not
accumulate these, since a stale claim is reclaimable by any subsequent
request against the same key.

**Remediate:** if it's a client retry storm, this is expected behavior
working as designed (fail-fast 409 instead of queueing behind the first
request) -- coordinate with the calling team about their retry policy. If
`in_progress` rows are genuinely stuck well past the TTL, check
`fly logs` for whatever's causing requests against those keys to hang. There
is no manual "release" needed -- the TTL-based reclaim in `claim_key` handles
stale locks automatically once a new request comes in for the same key.

**Verify recovery:** `rate(idempotency_conflicts_total[5m])` back under the
1/s threshold, sustained.

## LedgerlineReconciliationUnresolved

**What fired:** `reconciliation_findings_total{type="missing_settlement"}`
has been non-zero for over an hour. Unlike delivery/worker alerts, this
never resolves itself -- resolution is always an explicit operator action
(`ReconciliationResolution`), so a sustained non-zero value is a genuine,
ledger-authoritative discrepancy waiting on a human.

**Confirm:**

```sh
curl -s https://<app>.fly.dev/v1/reconciliation/runs/<run-id>/findings?resolution=unresolved \
  -H "Authorization: Bearer <key>"
```

or the dashboard's reconciliation panel (`GET /dashboard`, open findings),
or directly:

```sql
SELECT id, finding_type, transaction_id, settlement_line_id, delta_amount, detail, created_at
FROM reconciliation_findings
WHERE finding_type = 'missing_settlement' AND resolution = 'unresolved'
ORDER BY created_at ASC;
```

**Remediate:** inspect each finding's `detail` and the referenced
transaction/settlement line to understand whether the settlement is simply
late (an upstream feed delay -- wait and re-run reconciliation) or genuinely
missing (funds that never settled). Resolve via:

```sh
curl -s -X POST https://<app>.fly.dev/v1/reconciliation/findings/<finding-id>/resolve \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"action": "post_adjustment", "note": "<why>"}'
```

`action` is `post_adjustment` (posts a real correcting ledger transaction
through `resolve_manual_adjustment`) or `suppress` (accepts the discrepancy
without posting money movement -- use when investigation concludes it's not
a real loss, e.g. a duplicate/erroneous settlement line). Both require a
`note` for the audit trail in practice, even though the schema only requires
it to be present when you want it recorded.

**Verify recovery:** the finding's `resolution` is no longer `unresolved`
(`GET .../findings` no longer lists it), and
`reconciliation_findings_total{type="missing_settlement"}` drops back toward
0 on the next `/metrics` scrape.

## Rollback procedure

A bad deploy (crash-looping app/worker, a regression caught right after
release): `fly releases --app ledgerline` lists prior releases by version;
`fly releases rollback --app ledgerline` (or, on older `flyctl` versions
without that subcommand, `fly deploy --image <previous-release's-image-ref>
--app ledgerline`, taking the image reference from `fly releases`'s output)
returns to the last known-good release. Run `fly help releases` to confirm
the exact subcommand your installed `flyctl` version supports before an
incident -- the CLI surface here has changed across versions.

`release_command = "alembic upgrade head"` (`fly.toml`) still runs on a
rollback the same as any deploy. If the bad release included a migration
that the rollback target doesn't expect, rolling back the *code* without
also rolling back the *schema* can leave the old code pointed at a newer
schema than it was written against -- check `alembic history` and consider
whether a compensating `alembic downgrade` is needed before rolling back the
image, on a case-by-case basis. There is no automatic schema rollback.

## Failed `release_command` / half-applied migration recovery

`fly.toml`'s `release_command = "alembic upgrade head"` runs in a temporary
machine before any traffic shifts; a non-zero exit aborts the deploy and the
previous release keeps serving traffic -- there is no partial-traffic window
where a half-migrated schema is live.

Every migration under `migrations/versions/` (`0001` through `0005`) is
plain DDL (`op.add_column`, `op.create_index`, `op.create_table`, etc.) with
no `CONCURRENTLY` index builds and no manual transaction-control calls --
Postgres wraps each one in a single transaction by default, so a failure
partway through one migration rolls back cleanly rather than leaving that
migration half-applied. A failure can still leave the database at an
*earlier* revision than `head` (whichever migration failed didn't commit,
but any earlier ones in the same `release_command` run already did) --
confirm with:

```sh
fly ssh console --app ledgerline -C "python -m alembic current"
```

then fix whatever caused the failure (a bad migration, a lock timeout
against a live table) and re-run `fly deploy` -- `alembic upgrade head` is
naturally idempotent from wherever the database's current revision actually
is. `/readyz`'s Alembic-head check is the second, continuous layer that
would catch a machine that somehow started serving traffic against a
stale schema.

## Stuck `delivering` webhook rows outside automatic recovery

`sweep_stale_claims` reclaims rows stuck `delivering` after
`webhook_stale_claim_seconds` (default 60s) automatically, every poll cycle
-- this should make a manually-stuck row rare (Phase 8 slices 2/3 closed the
gaps that used to let this happen indefinitely). If one is still stuck well
past that window (check `claimed_at`):

```sql
SELECT id, status, claimed_at, attempt_count, reclaim_count
FROM webhook_deliveries
WHERE status = 'delivering' AND claimed_at < now() - interval '5 minutes';
```

If the worker process is confirmed healthy (ruling out
`LedgerlineWorkerStalled`) but a specific row is still wedged, reset it by
hand the same way `sweep_stale_claims` would:

```sql
UPDATE webhook_deliveries
SET status = 'pending', claimed_at = NULL
WHERE id = '<delivery-id>' AND status = 'delivering';
```

Deliberately not incrementing `reclaim_count` here, matching
`sweep_stale_claims`'s own reasoning -- an operator manually clearing a row
after confirming the worker is fine is a different situation than an
automatic reclaim from a suspected crash, and shouldn't spend that budget.

## API key revocation under an incident

```sh
fly ssh console --app ledgerline -C "python -m ledger.admin.keys revoke --id <key-id>"
```

marks the key inactive in the database immediately. It does **not** stop
that key from authenticating immediately, though: `require_api_key`'s
lookup is cached per app instance (`ApiKeyCache`, `api_key_cache_ttl_seconds`,
default 30.0s, negative caching included) -- a key revoked mid-incident can
keep working against the cached instance for up to 30 seconds after the
`revoke` call returns. For a compromised key during an active incident,
where even 30 seconds matters, set `API_KEY_CACHE_TTL_SECONDS=0` (via
`fly secrets set` or a one-off `fly deploy` with the env var changed) to
force every request to hit the database directly, then revoke -- at the
cost of a DB round trip per authenticated request until the setting is
reverted.

**Verify:** after the cache TTL has elapsed (or immediately, with the TTL
set to 0), a request using the revoked key should get `401 Unauthenticated`
-- the same error every other auth failure mode returns, by design
(`docs/DECISIONS.md` Phase 7), so a 401 alone doesn't confirm *which* case
this is; cross-check `python -m ledger.admin.keys list` shows the key as
`revoked`.
