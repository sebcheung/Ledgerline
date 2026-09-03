"""Contract tests for the generated OpenAPI schema (SPEC.md §9, §12 Phase 7).

No DB required -- `create_app()` builds the FastAPI app and
`app.openapi()` walks its route tree without touching a session, the same
pattern `tests/unit/test_health.py` already relies on.

Enforces the split `docs/DECISIONS.md` (Phase 6, extended Phase 7) flagged
as a thing to verify rather than trust: every `/v1` operation requires the
API key security scheme, and `/healthz`, `/readyz`, `/metrics`, and every
`/dashboard/*` operation require none.
"""

from typing import Any

from ledger.api.main import create_app


def _schema() -> dict[str, Any]:
    return create_app().openapi()


def _all_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(path, method, operation) for every path/method FastAPI generated."""
    return [
        (path, method, operation)
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method in ("get", "post", "put", "patch", "delete")
    ]


def test_api_key_bearer_security_scheme_is_registered() -> None:
    schema = _schema()
    schemes = schema["components"]["securitySchemes"]
    assert "ApiKeyBearer" in schemes
    assert schemes["ApiKeyBearer"]["type"] == "http"
    assert schemes["ApiKeyBearer"]["scheme"] == "bearer"


def test_every_v1_operation_requires_the_api_key_security_scheme() -> None:
    schema = _schema()
    for path, method, operation in _all_operations(schema):
        if not path.startswith("/v1"):
            continue
        assert operation.get("security"), f"{method.upper()} {path} has no security requirement"
        scheme_names = {name for req in operation["security"] for name in req}
        assert "ApiKeyBearer" in scheme_names, f"{method.upper()} {path}"


def test_every_v1_operation_has_a_summary() -> None:
    schema = _schema()
    for path, method, operation in _all_operations(schema):
        if not path.startswith("/v1"):
            continue
        assert operation.get("summary"), f"{method.upper()} {path} has no summary"


def test_health_routes_have_no_security_requirement() -> None:
    # /metrics and /dashboard/* have include_in_schema=False and so never
    # appear here at all (see the test below) -- /healthz and /readyz are
    # the only unauthenticated surfaces the schema itself can assert about.
    schema = _schema()
    for path, method, operation in _all_operations(schema):
        if path.startswith("/v1"):
            continue
        assert not operation.get("security"), f"{method.upper()} {path} should be unauthenticated"


def test_metrics_and_dashboard_are_excluded_from_the_schema() -> None:
    # Both include_in_schema=False -- /metrics is scraped by Prometheus,
    # not read as API documentation, and every /dashboard/* route is HTML,
    # not a client contract to version (docs/DECISIONS.md Phase 6). Their
    # *lack* of an authentication requirement is still enforced -- via
    # tests/integration/test_auth.py, which drives real HTTP requests --
    # since neither shows up here to assert against.
    schema = _schema()
    assert "/metrics" not in schema["paths"]
    assert not any(p.startswith("/dashboard") for p in schema["paths"])
