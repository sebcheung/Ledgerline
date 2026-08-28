"""Helpers for identifying *which* database constraint fired inside a
SQLAlchemy `IntegrityError`.

asyncpg raises its own `UniqueViolationError`; SQLAlchemy's asyncpg dialect
wraps that in its own DBAPI-shim exception, which SQLAlchemy then wraps
again in `sqlalchemy.exc.IntegrityError`. The original asyncpg exception
(which carries `constraint_name` and `sqlstate`) ends up reachable via
`exc.orig`, and on some driver/SQLAlchemy version combinations one level
further via `exc.orig.__cause__`. We walk both. asyncpg's exception
attributes are dynamically set (not part of a typed public API), so this
uses `getattr(..., default)` throughout rather than `# type: ignore` --
mypy --strict has no way to know they exist, and a default gracefully
degrades to the substring fallback below instead of raising.
"""

from sqlalchemy.exc import DBAPIError

#: Postgres SQLSTATE for a unique_violation.
UNIQUE_VIOLATION_SQLSTATE = "23505"


def constraint_name_of(exc: DBAPIError) -> str | None:
    """Best-effort extraction of the failing constraint's name.

    Tries the structured asyncpg attribute first; only falls back to a
    substring search of the exception's string form if that attribute is
    unavailable. `posting.py`'s unit tests assert the *structured* path
    fires in practice, so the substring fallback never silently becomes the
    real mechanism.
    """
    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str) and name:
            return name

    # Last-resort fallback: some driver/version combinations may not expose
    # constraint_name at all. Detail strings from Postgres look like:
    #   duplicate key value violates unique constraint "transactions_idempotency_key_key"
    text = str(exc.orig) if exc.orig is not None else str(exc)
    if '"' in text:
        parts = text.split('"')
        if len(parts) >= 2:
            return parts[1]
    return None


def is_unique_violation(exc: DBAPIError) -> bool:
    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        sqlstate = getattr(candidate, "sqlstate", None)
        if sqlstate == UNIQUE_VIOLATION_SQLSTATE:
            return True
    return False
