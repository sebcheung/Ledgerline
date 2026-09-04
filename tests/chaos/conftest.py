"""Shared fixtures for the worker-crash chaos suite.

Reuses `tests/faults/conftest.py`'s fixtures rather than duplicating their
bodies -- `tests/chaos/` is its own directory (pytest fixture discovery
walks up the directory tree from a test's location, and `tests/faults/`
is a sibling, not an ancestor, of `tests/chaos/`), so those fixtures have
to be re-exported here to be visible to tests in this directory. Importing
a `@pytest_asyncio.fixture`-decorated function into a module's namespace is
enough for pytest to register it as a fixture available to tests in that
module/conftest -- the decorator, not the import site, is what makes it a
fixture. `_clean_database` is autouse in `tests/faults/conftest.py`; that
property lives on the function object itself, so it stays autouse for
`tests/chaos/` too once re-exported this way.
"""

from tests.faults.conftest import (  # noqa: F401 -- re-exported for fixture discovery
    _clean_database,
    backdate_claim,
    dispatcher,
    ledger_row_counts,
    mock_receiver,
    receiver_state,
    wired_delivery,
)
