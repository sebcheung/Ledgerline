"""HTML error containment for the `/dashboard` prefix (SPEC.md §12 Phase 6).

`register_error_handlers` (`ledger/api/errors.py`) installs *app-wide*
`application/problem+json` handlers. Left alone, a dashboard route's error
(a typo'd path, an unexpected exception) would render JSON into a browser --
and for an `HX-Request` fragment, htmx won't even swap a non-2xx response,
so the operator would see nothing happen at all. This module re-registers
the same exception types with a shim that delegates to the *original*
handler for any request outside `prefix`, so the `/v1` JSON contract stays
byte-identical, and renders an HTML page or fragment for anything under it.
"""

import logging
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from dashboard.templating import templates
from ledger.api.errors import PROBLEM_HANDLERS
from ledger.core.errors import LedgerError

logger = logging.getLogger(__name__)

_Handler = Callable[[Request, Exception], Awaitable[Response]]


def _status_and_detail(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, LedgerError):
        return exc.status, exc.detail
    if isinstance(exc, RequestValidationError):
        return 422, "The request failed validation."
    if isinstance(exc, StarletteHTTPException):
        detail = exc.detail
        return exc.status_code, detail if isinstance(detail, str) and detail else "Not found."
    # Bare Exception (the unhandled-exception catch-all): never expose
    # str(exc) to the browser -- the request_id is enough to correlate with
    # server logs.
    return 500, "An unexpected error occurred."


async def _render_html_error(request: Request, exc: Exception) -> Response:
    status, detail = _status_and_detail(exc)
    if status >= 500:
        logger.error("dashboard_error", exc_info=exc)
    else:
        logger.warning("dashboard_error", extra={"status": status})

    request_id = structlog.contextvars.get_contextvars().get("request_id")
    context = {"status": status, "detail": detail, "request_id": request_id}

    is_hx_request = request.headers.get("HX-Request") == "true"
    # htmx does not swap a non-2xx response body at all -- an HX-Request
    # error is served as 200 carrying an error fragment so the operator
    # actually sees it. The real status is logged above; a full-page load
    # (not from htmx) still gets the true status code.
    if is_hx_request:
        return templates.TemplateResponse(
            request=request, name="partials/_error.html", context=context, status_code=200
        )
    return templates.TemplateResponse(
        request=request, name="error.html", context=context, status_code=status
    )


def install_html_error_handlers(app: FastAPI, *, prefix: str) -> None:
    def _wrap(exc_type: type[Exception]) -> _Handler:
        original = PROBLEM_HANDLERS[exc_type]

        async def handler(request: Request, exc: Exception) -> Response:
            if not request.url.path.startswith(prefix):
                return await original(request, exc)
            return await _render_html_error(request, exc)

        return handler

    for exc_type in PROBLEM_HANDLERS:
        app.add_exception_handler(exc_type, _wrap(exc_type))
