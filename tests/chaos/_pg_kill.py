"""Real Postgres backend termination -- genuine infrastructure chaos, not a
monkeypatched exception (contrast `tests/faults/test_idempotency_crash.py`'s
`SimulatedCrash`/`BaseException` technique, or
`tests/chaos/test_worker_crash_after_success.py`'s equivalent for the
dispatcher). These tests need a connection to actually die mid-transaction,
the way it would under a real failover or an operator's `pg_terminate_
backend`, and then observe how the rest of the stack behaves.

The pattern mirrors `tests/faults/conftest.py`'s `backdate_*` helpers: run
the mutation from an *independent* connection/engine, never from the
connection under test itself.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


async def backend_pid(conn: AsyncConnection | AsyncSession) -> int:
    """The Postgres server backend PID serving `conn`'s current connection.

    Call this on the connection/session under test -- it runs on the same
    connection, so it reports exactly the backend that a subsequent
    `kill_backend` call (from a *different* connection) needs to target.
    """
    pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    return int(pid)


async def kill_backend(engine: AsyncEngine, pid: int) -> bool:
    """Terminate a Postgres backend by PID, from an independent connection
    on `engine`. Returns `pg_terminate_backend`'s own report: `True` if a
    backend with that PID existed and was signaled, `False` if it was
    already gone (e.g. a previous call already killed it, or it exited on
    its own) -- never raises just because the target is already dead.
    """
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        return bool(result.scalar_one())
