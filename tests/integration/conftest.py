import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_database(clean_database: None) -> None:
    """Every integration test gets a clean set of ledger tables. See
    `tests/conftest.py::clean_database` for why this isn't a root-level
    autouse fixture."""
