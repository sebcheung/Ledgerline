"""Cross-checks ops/alerts.yml's PromQL expressions against the metrics
actually registered in ledger.observability.metrics.REGISTRY (Phase 8 slice
4, docs/DECISIONS.md).

`promtool check rules` (run in CI, see .github/workflows/ci.yml) validates
that ops/alerts.yml is syntactically valid PromQL -- it has no idea what
metrics this codebase actually emits, so a metric rename that forgets to
update ops/alerts.yml would pass `promtool check rules` while silently
turning every alert referencing it into a permanently-`0` no-op. This test
is the guard for that case.

Metric name normalization: `REGISTRY.collect()` reports each metric's *base*
name -- prometheus_client strips a Counter's trailing "_total" from
`Metric.name` (e.g. "webhook_delivery_attempts", not
"webhook_delivery_attempts_total") but leaves it in place on Gauges that
happen to be named with a "_total" suffix (e.g. "webhook_deliveries_total"
stays as-is, since Gauge names aren't normalized). A PromQL expression, on
the other hand, always spells out whatever the exposition text says,
including "_total" and Histogram's "_bucket"/"_sum"/"_count" suffixes.
`_canonical` reduces both sides to the same base form so they can be
compared directly.
"""

import re
from pathlib import Path

import yaml

from ledger.observability.metrics import REGISTRY

ALERTS_PATH = Path(__file__).resolve().parents[2] / "ops" / "alerts.yml"

#: PromQL functions/keywords and label names/values that can appear in an
#: `expr` string in the shape of a metric name but are not one. Label
#: matchers (`{type="missing_settlement"}`) and `by (...)` clauses are
#: stripped from the expression text before tokenizing (see
#: `_metric_name_tokens`), which removes most of these on its own; this set
#: covers what's left: bare PromQL function/operator keywords.
_PROMQL_KEYWORDS = {
    "rate",
    "increase",
    "delta",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "offset",
    "and",
    "or",
    "unless",
    "bool",
    "histogram_quantile",
}

#: A metric-name-shaped token immediately preceded by a digit (e.g. the "m"
#: in "[5m]" or "h" in "[1h]") is a duration unit, not an identifier.
_TOKEN_RE = re.compile(r"(?<![0-9])[a-zA-Z_:][a-zA-Z0-9_:]*")

#: Histogram suffixes that appear in PromQL but not in `Metric.name`.
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _load_alert_exprs() -> list[str]:
    with ALERTS_PATH.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    exprs = []
    for group in doc["groups"]:
        for rule in group["rules"]:
            assert "expr" in rule, f"rule {rule.get('alert')!r} has no expr"
            exprs.append(rule["expr"])
    return exprs


def _canonical(name: str) -> str:
    """Strip Histogram component suffixes and a trailing Counter "_total",
    in that order, to get a name comparable across both sides."""
    for suffix in _HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.endswith("_total"):
        name = name[: -len("_total")]
    return name


def _metric_name_tokens(expr: str) -> set[str]:
    # Strip label matcher blocks (`{...}`) and `by (...)` / `without (...)`
    # aggregation clauses -- both hold label names/values, not metric names,
    # and would otherwise pollute the token set (e.g. `le`, `outcome`,
    # `"retried"`, `type`, `"missing_settlement"`).
    stripped = re.sub(r"\{[^}]*\}", " ", expr)
    stripped = re.sub(r"\b(?:by|without)\s*\([^)]*\)", " ", stripped)
    tokens = set(_TOKEN_RE.findall(stripped))
    return {t for t in tokens if t not in _PROMQL_KEYWORDS}


def _registered_canonical_names() -> set[str]:
    return {_canonical(metric.name) for metric in REGISTRY.collect()}


def test_alerts_file_parses_and_has_rules() -> None:
    exprs = _load_alert_exprs()
    assert len(exprs) >= 8


def test_every_metric_referenced_in_an_alert_expr_is_registered() -> None:
    registered = _registered_canonical_names()
    for expr in _load_alert_exprs():
        for token in _metric_name_tokens(expr):
            assert _canonical(token) in registered, (
                f"{token!r} (from expr {expr!r}) does not match any metric "
                f"registered in ledger.observability.metrics.REGISTRY -- "
                f"either the alert or the metric was renamed without "
                f"updating the other"
            )
