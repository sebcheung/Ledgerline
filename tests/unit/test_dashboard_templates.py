"""Unit tests for the dashboard's template environment (SPEC.md §12 Phase
6) -- no DB, no HTTP. The substitute for a template lint step ruff/mypy
can't provide: every `.html` must at least parse/compile, and the four
panel names (`dashboard.data.PANEL_NAMES`) must never drift from either
their `partials/_<name>.html` file or their `sse-swap="<name>"` attribute in
`index.html`.
"""

import uuid
from datetime import UTC, datetime

import pytest

from dashboard.data import PANEL_NAMES
from dashboard.templating import TEMPLATES_DIR, render_fragment, templates
from ledger.models.enums import AccountType
from ledger.readmodels.balances import AccountBalanceRow, BalancesSnapshot


def _discover_templates() -> list[str]:
    return sorted(
        str(p.relative_to(TEMPLATES_DIR)).replace("\\", "/") for p in TEMPLATES_DIR.rglob("*.html")
    )


_TEMPLATE_NAMES = _discover_templates()


def test_templates_dir_resolves() -> None:
    assert TEMPLATES_DIR.is_dir()
    assert (TEMPLATES_DIR / "index.html").is_file()


def test_discovered_at_least_one_template() -> None:
    # Guards against `_discover_templates` silently returning an empty list
    # forever (e.g. a bad glob) and every parametrized test below passing
    # vacuously.
    assert _TEMPLATE_NAMES


@pytest.mark.parametrize("name", _TEMPLATE_NAMES)
def test_template_compiles(name: str) -> None:
    templates.get_template(name)


@pytest.mark.parametrize("panel", PANEL_NAMES)
def test_every_panel_has_a_fragment_template(panel: str) -> None:
    assert (TEMPLATES_DIR / "partials" / f"_{panel}.html").is_file()


@pytest.mark.parametrize("panel", PANEL_NAMES)
def test_every_panel_has_a_matching_sse_swap_in_index(panel: str) -> None:
    index_source = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert f'sse-swap="{panel}"' in index_source


def test_balances_fragment_escapes_account_name() -> None:
    row = AccountBalanceRow(
        id=uuid.uuid4(),
        name="<script>alert(1)</script>",
        type=AccountType.ASSET,
        currency="USD",
        allow_negative=False,
        is_suspense=False,
        is_clearing=False,
        balance=0,
        entry_count=0,
        updated_at=datetime.now(UTC),
    )

    snapshot = BalancesSnapshot(accounts=[row], totals_by_currency=[])
    html = render_fragment("partials/_balances.html", {"balances": snapshot})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
