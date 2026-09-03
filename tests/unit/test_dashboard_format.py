"""Unit tests for `dashboard.format` -- no DB, no HTTP. Pure formatting
helpers registered as Jinja filters."""

import pytest

from dashboard.format import (
    format_countdown,
    format_minor_units,
    format_relative_age,
    status_badge_class,
)
from ledger.models.enums import ReconciliationResolution, WebhookDeliveryStatus


def test_format_minor_units_usd_two_decimals() -> None:
    assert format_minor_units(1234, "USD") == "12.34 USD"
    assert format_minor_units(0, "USD") == "0.00 USD"


def test_format_minor_units_negative_keeps_sign() -> None:
    assert format_minor_units(-500, "USD") == "-5.00 USD"


def test_format_minor_units_respects_currency_exponent() -> None:
    # JPY has no minor unit (exponent 0) -- reuses ledger.core.money's
    # exponent table rather than hardcoding 2 decimal places everywhere.
    assert format_minor_units(1234, "JPY") == "1234 JPY"
    # KWD has a 3-digit minor unit.
    assert format_minor_units(1234, "KWD") == "1.234 KWD"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "due now"),
        (-5, "due now"),
        (1, "in 1s"),
        (59, "in 59s"),
        (60, "in 1m 0s"),
        (61, "in 1m 1s"),
        (3599, "in 59m 59s"),
        (3600, "in 1h 0m"),
        (3601, "in 1h 0m"),
    ],
)
def test_format_countdown_boundaries(seconds: int, expected: str) -> None:
    assert format_countdown(seconds) == expected


def test_format_countdown_none_is_em_dash() -> None:
    assert format_countdown(None) == "—"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "just now"),
        (59, "just now"),
        (60, "1m ago"),
        (3599, "59m ago"),
        (3600, "1h ago"),
        (86399, "23h ago"),
        (86400, "1d ago"),
    ],
)
def test_format_relative_age_boundaries(seconds: int, expected: str) -> None:
    assert format_relative_age(seconds) == expected


@pytest.mark.parametrize("status", list(WebhookDeliveryStatus))
def test_status_badge_class_covers_every_webhook_status(status: WebhookDeliveryStatus) -> None:
    # A plain dict lookup with no catch-all default -- adding a new enum
    # member without a matching badge class must fail this test (KeyError),
    # not silently render an unstyled badge.
    css_class = status_badge_class(status)
    assert css_class.startswith("badge badge-")


@pytest.mark.parametrize("resolution", list(ReconciliationResolution))
def test_status_badge_class_covers_every_reconciliation_resolution(
    resolution: ReconciliationResolution,
) -> None:
    css_class = status_badge_class(resolution)
    assert css_class.startswith("badge badge-")
