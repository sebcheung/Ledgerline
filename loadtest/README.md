# Load testing (SPEC.md §12 Phase 7)

`locustfile.py` drives `LedgerlineUser` against a running Ledgerline
instance: posting balanced transactions (both per-user account pairs and a
shared, deliberately contended pair -- see the file's docstring for why
both matter), replaying an idempotent request, reading accounts, and
listing transactions.

Not part of the wheel or the Docker image (see `[tool.hatch.build.targets.
wheel]` in `pyproject.toml` and the `Dockerfile`'s `COPY` list), not
installed by `.[dev]`, and **not run in CI** -- a throughput/p99 assertion
on a shared GitHub Actions runner would be either so loose it proves
nothing or so tight it fails on every unrelated infrastructure hiccup.
Run it manually, against docker-compose or a real deployment, and record
what you observed below.

## Running it

```sh
pip install -e ".[loadtest]"

# 1. Start the stack and mint a key.
docker compose up -d
python -m ledger.admin.keys mint --name loadtest

# 2. The default rate limit (100 req/s, burst 200 per key) will throttle a
#    single-key load test long before you learn anything about the ledger
#    itself. Either raise it for this run:
RATE_LIMIT_RPS=100000 RATE_LIMIT_BURST=100000 docker compose up -d app
#    ...or run a second pass at the *default* limit specifically to
#    measure the 429 rate -- that is the load-shedding proof, not a bug.

# 3. Run Locust.
LEDGERLINE_API_KEY=<key> locust -f loadtest/locustfile.py \
    --host http://localhost:8000 --headless -u 50 -r 10 --run-time 60s \
    --csv loadtest-results
```

Cross-check the reported `POST /v1/transactions` p99 against
`posting_latency_seconds` on `GET /metrics` from the same run -- the delta
is framework, auth, and idempotency overhead sitting in front of the
domain operation SPEC.md §9 names.

## Observed results

| Date | Where | Users | Duration | Throughput | p50 | p99 | 429 rate |
|---|---|---|---|---|---|---|---|
| 2026-09-03 | Local (Docker Desktop, Windows, shared-cpu dev machine), `docker compose`, rate limit disabled | 5 (ramp 5/s) | 15s | ~28 req/s aggregate | 39ms (all endpoints) | 130ms (`POST /v1/transactions`) | 0% |
| 2026-09-03 | Local (Docker Desktop, Windows, shared-cpu dev machine), `docker compose`, rate limit disabled | 50 (ramp 10/s) | 60s | ~104 req/s aggregate | 320ms (all endpoints) | 670ms (all endpoints), 710ms (`POST /v1/transactions`) | 0% |

The 50-user run's `posting_latency_seconds` histogram on `/metrics` for the
same run put the domain operation itself (`ledger.core.posting.post_transaction`,
excluding auth, rate limiting, and idempotency middleware) at p99 under
250ms across 5458 postings -- the ~450ms gap to the end-to-end p99 above is
that surrounding overhead, not the ledger write.

Both runs are against a single-machine `docker compose` stack on a
shared-cpu dev laptop, not a production-shaped deployment -- re-run against
real infrastructure before treating either as a capacity ceiling.
