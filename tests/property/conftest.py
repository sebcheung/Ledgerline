import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_database(clean_database: None) -> None:
    """Property tests truncate again per-example inside the test body itself
    (one pytest invocation covers many Hypothesis examples); this fixture
    only guarantees a clean slate before the *first* example."""
