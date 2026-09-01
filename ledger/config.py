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


@lru_cache
def get_settings() -> Settings:
    return Settings()
