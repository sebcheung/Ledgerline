from fastapi import FastAPI

from ledger.api.errors import register_error_handlers
from ledger.api.health import router as health_router
from ledger.api.routes.accounts import router as accounts_router
from ledger.api.routes.admin import router as admin_router
from ledger.api.routes.reconciliation import router as reconciliation_router
from ledger.api.routes.settlements import router as settlements_router
from ledger.api.routes.transactions import router as transactions_router
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
    register_error_handlers(app)
    return app


app = create_app()
