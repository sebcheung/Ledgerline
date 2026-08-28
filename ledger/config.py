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


@lru_cache
def get_settings() -> Settings:
    return Settings()
