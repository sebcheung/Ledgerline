from fastapi import FastAPI

from ledger.api.health import router as health_router
from ledger.observability.logging import configure_logging
from ledger.observability.middleware import RequestIdMiddleware


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Ledgerline")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
