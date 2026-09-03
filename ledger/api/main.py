from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ledger.api.auth import ApiKeyCache
from ledger.api.deps import V1_DEPENDENCIES
from ledger.api.errors import register_error_handlers
from ledger.api.health import router as health_router
from ledger.api.openapi import APP_DESCRIPTION, PROBLEM_RESPONSES, TAGS_METADATA
from ledger.api.ratelimit import RateLimiter
from ledger.api.routes.accounts import router as accounts_router
from ledger.api.routes.admin import router as admin_router
from ledger.api.routes.metrics import router as metrics_router
from ledger.api.routes.reconciliation import router as reconciliation_router
from ledger.api.routes.settlements import router as settlements_router
from ledger.api.routes.transactions import router as transactions_router
from ledger.api.routes.webhooks import router as webhooks_router
from ledger.config import get_settings
from ledger.observability.logging import configure_logging
from ledger.observability.middleware import RequestIdMiddleware


def _app_version() -> str:
    try:
        return version("ledgerline")
    except PackageNotFoundError:
        # e.g. running from a checkout without `pip install -e .` -- keep
        # OpenAPI generation working rather than raising during startup.
        return "0.0.0+unknown"


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Ledgerline",
        version=_app_version(),
        description=APP_DESCRIPTION,
        summary="An idempotent double-entry payments ledger and reconciliation engine.",
        openapi_tags=TAGS_METADATA,
    )
    # Phase 7: one instance per app, not a module-level global -- see
    # `ApiKeyCache`'s and `RateLimiter`'s docstrings. Every `create_app()`
    # call in the test suite therefore starts with an empty cache and full
    # buckets, never inheriting state from another test's app.
    app.state.api_key_cache = ApiKeyCache(ttl_seconds=settings.api_key_cache_ttl_seconds)
    app.state.rate_limiter = RateLimiter(
        rate_per_second=settings.rate_limit_rps, burst=settings.rate_limit_burst
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    # Phase 7: no V1_DEPENDENCIES -- /metrics is unauthenticated by
    # construction, same as /healthz and /readyz (see
    # ledger/api/routes/metrics.py).
    app.include_router(metrics_router)
    app.include_router(
        accounts_router,
        prefix="/v1",
        tags=["accounts"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    app.include_router(
        transactions_router,
        prefix="/v1",
        tags=["transactions"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    app.include_router(
        admin_router,
        prefix="/v1",
        tags=["admin"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    app.include_router(
        settlements_router,
        prefix="/v1",
        tags=["settlements"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    app.include_router(
        reconciliation_router,
        prefix="/v1",
        tags=["reconciliation"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    app.include_router(
        webhooks_router,
        prefix="/v1",
        tags=["webhooks"],
        dependencies=V1_DEPENDENCIES,
        responses=PROBLEM_RESPONSES,
    )
    register_error_handlers(app)

    # Phase 6: an HTML operator console, deliberately outside `/v1` -- it
    # has no client contract to version, and (Phase 7, see
    # docs/DECISIONS.md) sits outside API key auth: it isn't given
    # `V1_DEPENDENCIES`, so the exclusion is structural rather than an
    # allowlist. Mounted into this app rather than a second FastAPI() so it
    # reuses RequestIdMiddleware, the error handlers above, and the
    # app_client test fixture's dependency_overrides (which do not
    # propagate into a mounted sub-application).
    if get_settings().dashboard_enabled:
        from dashboard.errors import install_html_error_handlers
        from dashboard.templating import STATIC_DIR
        from dashboard.views import router as dashboard_router

        app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
        app.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
        install_html_error_handlers(app, prefix="/dashboard")

    return app


app = create_app()
