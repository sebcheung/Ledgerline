# Deploying Ledgerline to Fly.io

`fly.toml` and `.github/workflows/cd.yml` are deploy-*ready*, not deployed:
no `fly launch` has been run, no Fly app or managed Postgres cluster exists,
and no secrets are set. CD is a clean no-op until you complete the one-time
setup below.

## One-time setup

1. **Create the app** (does not deploy):

   ```sh
   fly launch --no-deploy --copy-config --name ledgerline
   ```

2. **Provision managed Postgres and attach it**:

   ```sh
   fly postgres create --name ledgerline-db
   fly postgres attach ledgerline-db --app ledgerline
   ```

   `fly postgres attach` sets the `DATABASE_URL` secret directly on the app,
   in libpq form (`postgres://...`). `Settings._normalize_database_url`
   (`ledger/config.py`) rewrites this to `postgresql+asyncpg://...` at
   startup -- no manual edit needed.

3. **First deploy**:

   ```sh
   fly deploy
   ```

   `release_command = "alembic upgrade head"` (`fly.toml`) runs in a
   temporary machine with the app's secrets attached, before any traffic
   shifts to the new release; a non-zero exit aborts the deploy.

4. **Mint the first API key**, from inside the running app machine (API key
   hashes are never stored anywhere retrievable -- see
   `ledger/core/apikeys.py`):

   ```sh
   fly ssh console -C "python -m ledger.admin.keys mint --name first-key"
   ```

5. **Create the retention sweep as a scheduled machine** (SPEC.md §9's
   `idempotency_keys` retention, `ledger.admin.sweep`):

   ```sh
   fly machine run . --schedule daily -C "python -m ledger.admin.sweep" --app ledgerline
   ```

6. **Enable CD**: add a `FLY_API_TOKEN` repository secret
   (`fly tokens create deploy --app ledgerline`) under Settings → Secrets
   and variables → Actions. `.github/workflows/cd.yml` deploys on every
   push to `main` that CI passes, once this secret exists -- before that,
   every step in the workflow is skipped.

## Notes

- Exactly one web machine, running exactly one uvicorn worker
  (`--workers 1` in `fly.toml`): the in-process rate limiter
  (`ledger/api/ratelimit.py`) and in-process metric counters
  (`ledger/observability/metrics.py`) are only correct for a single
  process -- see `docs/DECISIONS.md` Phase 7.
- `DEMO_ENABLED` is `false` in `fly.toml`'s `[env]` and is refused outright
  in production regardless (`dashboard/views.py`) -- the demo scenario
  writes real ledger rows and registers webhook endpoints pointed at
  deliberately failing URLs.
- `GET /metrics` and `GET /healthz`/`GET /readyz` are unauthenticated by
  construction (`ledger/api/main.py`); Fly's built-in Prometheus scraper
  (`[[metrics]]` in `fly.toml`) polls `/metrics` over the private network
  and cannot send an `Authorization` header.
