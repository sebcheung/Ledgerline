"""Shared API key seeding for test client fixtures (Phase 7).

`tests/conftest.py::app_client` and `tests/faults/conftest.py::fault_client`
both call `seed_api_key` so every existing integration/fault test keeps
passing under Phase 7's auth requirement while genuinely exercising the
shipped code path -- see `docs/DECISIONS.md` Phase 7 for why this was
chosen over a settings-based auth kill switch or a dependency override.
"""

import uuid

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.core.apikeys import hash_api_key
from ledger.models.api_keys import ApiKey

#: A fixed raw key (not `generate_api_key()`) so every test client can send
#: the exact same `Authorization` header without threading a fixture value
#: through every test file.
TEST_API_KEY = "lk_test_ledgerline_integration_suite"


async def seed_api_key(
    engine: AsyncEngine,
    *,
    raw_key: str = TEST_API_KEY,
    name: str = "test-suite",
    active: bool = True,
) -> uuid.UUID:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(ApiKey)
                .values(key_hash=hash_api_key(raw_key), name=name, active=active)
                .returning(ApiKey.id)
            )
        ).one()
        key_id: uuid.UUID = row.id
        return key_id
