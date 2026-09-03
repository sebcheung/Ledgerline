"""Unit tests for `Settings._normalize_database_url` (SPEC.md §12 Phase 7:
Fly.io deploy with managed Postgres). No DB required -- constructs
`Settings` directly rather than going through `get_settings()`'s
env-file-backed cache."""

from ledger.config import Settings


def _settings(url: str) -> Settings:
    return Settings(database_url=url)


def test_bare_postgres_scheme_is_rewritten_to_asyncpg() -> None:
    assert (
        _settings("postgres://u:p@host:5432/db").database_url
        == "postgresql+asyncpg://u:p@host:5432/db"
    )


def test_bare_postgresql_scheme_is_rewritten_to_asyncpg() -> None:
    assert (
        _settings("postgresql://u:p@host:5432/db").database_url
        == "postgresql+asyncpg://u:p@host:5432/db"
    )


def test_already_asyncpg_url_is_left_untouched() -> None:
    url = "postgresql+asyncpg://u:p@host:5432/db"
    assert _settings(url).database_url == url


def test_an_explicit_non_asyncpg_driver_is_left_untouched() -> None:
    url = "postgresql+psycopg://u:p@host:5432/db"
    assert _settings(url).database_url == url
