"""Pure, framework-free formatting helpers registered as Jinja filters by
`dashboard/templating.py`.

Zero I/O and zero database access -- the one thing separating these from
`ledger.readmodels` is that they may reuse `ledger.core.money`'s currency
exponent table, never that they touch a session. Kept in `dashboard/`
because they're presentation, not domain logic.
"""

from ledger.core.money import Money
from ledger.models.enums import ReconciliationResolution, WebhookDeliveryStatus

_WEBHOOK_BADGE_CLASS: dict[WebhookDeliveryStatus, str] = {
    WebhookDeliveryStatus.PENDING: "badge badge-pending",
    WebhookDeliveryStatus.DELIVERING: "badge badge-active",
    WebhookDeliveryStatus.SUCCEEDED: "badge badge-success",
    WebhookDeliveryStatus.DEAD: "badge badge-danger",
}

_RESOLUTION_BADGE_CLASS: dict[ReconciliationResolution, str] = {
    ReconciliationResolution.UNRESOLVED: "badge badge-danger",
    ReconciliationResolution.AUTO_RESOLVED: "badge badge-success",
    ReconciliationResolution.MANUALLY_RESOLVED: "badge badge-success",
    ReconciliationResolution.SUPPRESSED: "badge badge-pending",
}


def format_minor_units(amount: int, currency: str) -> str:
    """Render a bigint minor-units amount as a decimal string with its
    currency's own exponent (SPEC.md non-goal: no FX, but JPY/KWD-style
    exponents still matter for display) -- reuses `ledger.core.money.Money`,
    the single source of truth for that exponent table, rather than
    hardcoding 2 decimal places."""
    return f"{Money(amount, currency).to_decimal_string()} {currency}"


def format_countdown(seconds: int | None) -> str:
    """`None` -- no pending retry (e.g. a `succeeded` row). `<= 0` -- already
    due; the dispatcher's next poll will pick it up. Otherwise a compact
    `Xh Ym` / `Xm Ys` / `Xs` countdown."""
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "due now"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"in {hours}h {minutes}m"
    if minutes:
        return f"in {minutes}m {secs}s"
    return f"in {secs}s"


def format_relative_age(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def status_badge_class(status: WebhookDeliveryStatus | ReconciliationResolution) -> str:
    """Deliberately a plain dict lookup per enum, not a catch-all default --
    a `KeyError` here on a newly-added enum member is exactly the loud
    failure `tests/unit/test_dashboard_format.py` pins (an unstyled badge
    silently rendering is the alternative, and it's worse)."""
    if isinstance(status, WebhookDeliveryStatus):
        return _WEBHOOK_BADGE_CLASS[status]
    return _RESOLUTION_BADGE_CLASS[status]
