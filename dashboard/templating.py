"""The dashboard's Jinja2 template environment and static asset directory
(SPEC.md §12 Phase 6).

Resolved from `Path(__file__)`, never a CWD-relative path -- `Dockerfile`
installs a wheel (`pip install .`), so a relative `"dashboard/templates"`
would only happen to work today because the image also copies the source
tree next to it. The same idiom `ledger/api/health.py` already uses for
`alembic.ini`.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from dashboard.format import (
    format_countdown,
    format_minor_units,
    format_relative_age,
    status_badge_class,
)

_DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
STATIC_DIR = _DASHBOARD_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["minor_units"] = format_minor_units
templates.env.filters["countdown"] = format_countdown
templates.env.filters["relative_age"] = format_relative_age
templates.env.filters["badge_class"] = status_badge_class


def render_fragment(name: str, context: dict[str, object]) -> str:
    """Render a partial outside of a request/response cycle. Used by
    `dashboard/sse.py`, which has no `Request` to hand `TemplateResponse` --
    every SSE tick opens and closes its own session and has nothing else in
    common with an HTTP request/response pair."""
    return templates.get_template(name).render(context)
