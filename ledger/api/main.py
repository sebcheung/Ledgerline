from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ledger.api.errors import register_error_handlers
from ledger.api.health import router as health_router
from ledger.api.routes.accounts import router as accounts_router
from ledger.api.routes.admin import router as admin_router
from ledger.api.routes.reconciliation import router as reconciliation_router
from ledger.api.routes.settlements import router as settlements_router
from ledger.api.routes.transactions import router as transactions_router
from ledger.api.routes.webhooks import router as webhooks_router
from ledger.config import get_settings
from ledger.observability.logging import configure_logging
from ledger.observability.middleware import RequestIdMiddleware


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Ledgerline")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    app.include_router(accounts_router, prefix="/v1", tags=["accounts"])
    app.include_router(transactions_router, prefix="/v1", tags=["transactions"])
    app.include_router(admin_router, prefix="/v1", tags=["admin"])
    app.include_router(settlements_router, prefix="/v1", tags=["settlements"])
    app.include_router(reconciliation_router, prefix="/v1", tags=["reconciliation"])
    app.include_router(webhooks_router, prefix="/v1", tags=["webhooks"])
    register_error_handlers(app)

    # Phase 6: an HTML operator console, deliberately outside `/v1` -- it
    # has no client contract to version and (Phase 7) will sit outside API
    # key auth, unlike every route above. Mounted into this app rather than
    # a second FastAPI() so it reuses RequestIdMiddleware, the error
    # handlers above, and the app_client test fixture's
    # dependency_overrides (which do not propagate into a mounted
    # sub-application).
    if get_settings().dashboard_enabled:
        from dashboard.errors import install_html_error_handlers
        from dashboard.templating import STATIC_DIR
        from dashboard.views import router as dashboard_router

        app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
        app.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
        install_html_error_handlers(app, prefix="/dashboard")

    return app


app = create_app()
