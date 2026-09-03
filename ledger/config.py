from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    log_level: str = "INFO"
    environment: str = "development"

    # Used starting Phase 3 (idempotency layer). Defined here now so config
    # is a single settings surface for the whole project.
    idempotency_lock_ttl_seconds: int = 30

    # Used starting Phase 4 (reconciliation engine).
    recon_window_days: int = 7
    recon_cutoff_lag_hours: int = 24
    recon_auto_resolve_threshold_minor: int = 500
    # SPEC.md §7 pass 3 hardcodes "±2 days" for the fuzzy match; pulled into
    # config so the matcher's date-distance check and the candidate-set
    # widening that has to agree with it can't drift apart.
    recon_fuzzy_days: int = 2
    # A reconciliation run can legitimately take longer than the default
    # idempotency TTL (30s) once a window has any real volume in it. A
    # dedicated, longer TTL for POST /v1/reconciliation/runs avoids the
    # run's own success being rolled back by a concurrent retry that thinks
    # the lock went stale mid-run. See docs/DECISIONS.md Phase 4.
    recon_run_lock_ttl_seconds: int = 300

    # Used starting Phase 5 (outbox fan-out + webhook dispatcher, SPEC.md §8).
    webhook_poll_interval_seconds: float = 1.0
    webhook_batch_size: int = 100
    webhook_fanout_batch_size: int = 500
    webhook_max_attempts: int = 8
    webhook_base_delay_seconds: float = 1.0
    webhook_max_delay_seconds: float = 3600.0
    # SPEC.md §8's "semaphore of 10" -- the per-cycle bound on concurrent
    # outbound POSTs, not a connection-pool size.
    webhook_concurrency: int = 10
    webhook_stale_claim_seconds: int = 60
    webhook_connect_timeout_seconds: float = 5.0
    webhook_read_timeout_seconds: float = 5.0

    # Used starting Phase 6 (dashboard + demo scenario, SPEC.md §12).
    dashboard_enabled: bool = True
    dashboard_sse_interval_seconds: float = 2.0
    dashboard_sse_keepalive_seconds: float = 15.0
    # A hard lifetime cap on one SSE stream. The browser's EventSource
    # reconnects automatically, so ending the stream is invisible to the
    # user and bounds any generator a missed disconnect would otherwise leak.
    dashboard_sse_max_stream_seconds: float = 3600.0
    dashboard_recent_transactions_limit: int = 25
    dashboard_recent_runs_limit: int = 10
    dashboard_queue_limit: int = 50

    # The demo scenario writes real ledger rows and registers webhook
    # endpoints pointed at deliberately failing URLs -- off by default, and
    # refused outright in production regardless of this flag (see
    # dashboard/views.py).
    demo_enabled: bool = False
    demo_retry_endpoint_url: str = "http://127.0.0.1:9/hook"
    demo_self_base_url: str = "http://127.0.0.1:8000"

    # Used starting Phase 7 (API key auth, SPEC.md §9). No kill switch, by
    # design: unlike rate limiting, disabling auth degrades security, not
    # just availability, and this is a payments API (see docs/DECISIONS.md
    # Phase 7). `0` disables only the cache, forcing every request to hit
    # the database -- useful for immediate revocation.
    api_key_cache_ttl_seconds: float = 30.0

    # Used starting Phase 7 (rate limiting, SPEC.md §9): 100 req/s, burst
    # 200, per API key, in-process (see docs/DECISIONS.md Phase 7 for the
    # multi-worker caveat this implies for fly.toml). Unlike auth, a kill
    # switch is defensible here -- it degrades availability, not security --
    # and the Locust load test needs one for a clean, unthrottled run.
    rate_limit_enabled: bool = True
    rate_limit_rps: float = 100.0
    rate_limit_burst: float = 200.0

    # Used starting Phase 7 (GET /metrics, SPEC.md §9). Deliberately
    # unauthenticated (see ledger/api/routes/metrics.py) -- Fly's built-in
    # Prometheus scraper polls over the private network and cannot send an
    # Authorization header. This flag is the only way to turn it off.
    metrics_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
