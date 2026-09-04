# Chaos testing (SPEC.md §12 Phase 8)

`tests/chaos/` proves the ledger's crash-recovery contracts against two
distinct fault categories: a webhook worker that crashes or is cancelled
mid-delivery (Phase 8 slice 2), and a real Postgres backend killed
mid-transaction, via `pg_terminate_backend` -- not a monkeypatched exception
-- both mid-posting and mid-idempotent-claim (Phase 8 slice 3). This
document records what running those faults actually measured, the same way
`loadtest/README.md` records observed load-test numbers instead of asserting
thresholds in CI.

## Running the suite

```sh
pytest tests/chaos/ -v
```

Needs a real Postgres, the same as `tests/faults/` -- either set
`DATABASE_URL` (CI does this against a service container) or let
`tests/conftest.py::database_url` spin up a throwaway one via
`testcontainers` for local runs. Not CI-gated on timing (only on
pass/fail): these tests assert recovery *happens* and invariants hold, never
assert *how fast*, for the same reason `loadtest/`'s Locust run isn't
CI-gated either -- a shared runner's wall-clock timing proves nothing about
a production deployment.

## Observed results

| Date | Scenario | Fault injected | Detected | Recovered | Duplicate deliveries | Ledger delta |
|---|---|---|---|---|---|---|
| 2026-09-04 | Worker crash mid-delivery (live `docker compose`) | `docker kill -s SIGKILL` on `webhook-worker` while a delivery sat in `delivering`, receiver deliberately slow (~4.5s) so the kill lands strictly inside the in-flight POST | Row frozen `status='delivering'`, `claimed_at` unchanged, confirmed by direct query immediately after the kill | `sweep_stale_claims` reclaimed it on the worker's first poll cycle after restart and redelivered successfully; ~14.2s wall-clock from the original `POST /v1/transactions` to successful webhook receipt (~11.6s from the kill itself, ~1.24s from the worker container's restart) | 1 (the receiver's application code observed the same `X-Ledgerline-Event-Id` twice: once as the crashed attempt's body, received before the kill even though the response was never read back, and once on the successful redelivery -- exactly the at-least-once contract `README.md` documents) | 0 (one `transactions` row, one set of balanced entries; the crash was entirely on the delivery side, never touched posting) |
| 2026-09-04 | Real Postgres backend loss, mid-posting and mid-idempotent-claim (automated, `tests/chaos/`) | `pg_terminate_backend()` against the exact backend PID serving the request's connection, fired from `posting._lock_account_balances` right after the real `FOR UPDATE` lock is acquired -- strictly before the entries insert, balance update, or commit | Both `test_db_loss_mid_posting.py` and `test_db_loss_mid_idempotent_request.py` assert the killed connection surfaces as a clean RFC 7807 500, not a raw traceback | All 8 tests in `tests/chaos/` passed in ~15.8s (`8 passed in 15.78s`, local run against a throwaway `postgres:16` container) | n/a (no webhook path in these scenarios) | 0 in every case: `ledger_row_counts` before/after is identical after the killed-mid-posting case (Postgres's own rollback-on-disconnect discarded the half-written transaction, entries, and balance update); the idempotent-claim case additionally confirms the `in_progress` claim is cleanly released (not left stuck for the TTL) and an immediate retry produces exactly one ledger effect |

Where: both rows are from a single-machine Windows dev workstation running
Docker Desktop, not production-shaped infrastructure -- the worker-crash row
against `docker compose up -d` (this repo's `docker-compose.yml`, default
service topology) with a WEBHOOK_STALE_CLAIM_SECONDS override to 10s
(production default: 60s) purely to keep a manual, hand-timed demonstration
under a minute; the DB-loss row is the automated suite's own pass/fail and
wall-clock, not a separate manual run.

The worker-crash row's absolute recovery time (~14s) is an artifact of that
10-second stale-claim override plus the ~1s default poll interval, not a
number to extrapolate into a production SLA -- at the shipped default of
`WEBHOOK_STALE_CLAIM_SECONDS=60`, the same crash would take up to ~61s to
recover instead, dominated entirely by the stale-claim window rather than by
anything in the recovery mechanism itself.

`webhook_stale_claims_swept_total` and `webhook_delivery_attempts_total`/
`webhook_delivery_latency_seconds` are incremented inside
`ledger.webhooks.dispatcher.Dispatcher`, which runs in the separate
`webhook-worker` process -- at the time this particular run was measured,
that process had no HTTP server of its own to scrape, so these counters
could not be cross-checked against a `/metrics` endpoint and the numbers
above come from direct `webhook_deliveries` row state and the receiver's
own request log instead. This measurement is exactly what surfaced the gap:
discovering that these Counters/Histogram had no scrape path at all (a
Counter's state is process-local; `GET /metrics` on the `app` process can
never see what `worker/webhook_worker.py` increments) meant two of
`ops/alerts.yml`'s eight alerts (`LedgerlineStaleClaimStorm`,
`LedgerlineDeliveryFailureRatio`) would have read a permanently-flat zero
against the real deployed topology. That gap is now fixed within this same
slice -- `worker/webhook_worker.py` runs its own second
`prometheus_client` HTTP server (port 9090 by default) against the same
registry, scraped as a second `fly.toml` `[[metrics]]` target (see
`docs/DECISIONS.md`'s Phase 8 entry) -- but this table's numbers still
reflect the pre-fix cross-check method used at measurement time, recorded
honestly rather than back-filled. `outbox_lag_seconds` (refreshed from the
database by the API process, visible on the `app` process's `/metrics`
regardless) remains the cross-process "is the worker alive at all" signal
an operator watches first, per `docs/RUNBOOK.md#ledgerlineworkerstalled`.

Neither row should be read as a capacity or latency ceiling -- re-run
against real infrastructure, and at the shipped configuration defaults,
before treating either number as representative of anything beyond "the
recovery path works and produces the documented at-least-once/zero-partial-
write guarantees."
