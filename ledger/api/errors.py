"""RFC 7807 `application/problem+json` error rendering.

`type` URIs are kept exactly as SPEC.md §9 writes them -- relative
references, not resolved against a configurable base URL. RFC 7807 permits
relative `type` values, and "stable" (as SPEC.md calls them) means not
varying per deployment host.

The single source of truth for the spec-named error types is the
`LedgerError` subclasses in `ledger.core.errors`; this module adds only the
handlers for exceptions that are not domain errors (framework validation,
generic HTTPException, and the unhandled-exception catch-all).
"""

import http
import logging
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from ledger.core.errors import LedgerError

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

#: Reserved RFC 7807 member names; an exception's `extra` dict must never be
#: allowed to overwrite one of these.
_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})

_STATUS_TYPE: dict[int, tuple[str, str]] = {
    400: ("/errors/bad-request", "Bad Request"),
    404: ("/errors/not-found", "Not Found"),
    405: ("/errors/method-not-allowed", "Method Not Allowed"),
    422: ("/errors/validation-error", "Unprocessable Entity"),
    429: ("/errors/rate-limited", "Too Many Requests"),
    503: ("/errors/service-unavailable", "Service Unavailable"),
}


def problem_response(
    request: Request,
    *,
    type_: str,
    title: str,
    status: int,
    detail: str,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id is not None:
        body["request_id"] = request_id
    if extra:
        for key, value in extra.items():
            if key in _RESERVED_MEMBERS:
                logger.warning("dropping reserved problem member %r from extra", key)
                continue
            body[key] = value
    return JSONResponse(body, status_code=status, media_type=PROBLEM_CONTENT_TYPE, headers=headers)


async def _ledger_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, LedgerError)
    if exc.status >= 500:
        logger.error("ledger_error", exc_info=exc)
    else:
        logger.warning("ledger_error", extra={"error_type": exc.error_type})
    return problem_response(
        request,
        type_=exc.error_type,
        title=exc.title,
        status=exc.status,
        detail=exc.detail,
        extra=exc.as_problem_members(),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Strip `input`/`ctx` from each error: `input` echoes the raw request
    # body back into the response (and typically into logs/proxies), which
    # for a payments API can mean echoing an amount or idempotency key.
    errors = [{k: v for k, v in err.items() if k not in ("input", "ctx")} for err in exc.errors()]
    return problem_response(
        request,
        type_="/errors/validation-error",
        title="Validation Error",
        status=422,
        detail="Request body failed validation",
        extra={"errors": errors},
    )


async def _pydantic_validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # A response failing its own schema is a server bug, not a client
    # error -- never expose the underlying pydantic error to the caller.
    logger.error("response_validation_error", exc_info=exc)
    return problem_response(
        request,
        type_="/errors/internal",
        title="Internal Server Error",
        status=500,
        detail="An unexpected error occurred.",
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    type_, default_title = _STATUS_TYPE.get(
        status, (f"/errors/http-{status}", _status_phrase(status))
    )
    detail_value = exc.detail
    extra: dict[str, Any] = {}
    if isinstance(detail_value, dict):
        detail_str = str(detail_value.get("detail") or detail_value.get("error") or default_title)
        extra = {k: v for k, v in detail_value.items() if k not in ("detail", "error")}
    else:
        detail_str = str(detail_value) if detail_value else default_title

    headers = dict(exc.headers) if exc.headers else None
    return problem_response(
        request,
        type_=type_,
        title=default_title,
        status=status,
        detail=detail_str,
        extra=extra,
        headers=headers,
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", exc_info=exc)
    return problem_response(
        request,
        type_="/errors/internal",
        title="Internal Server Error",
        status=500,
        detail="An unexpected error occurred.",
    )


def _status_phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LedgerError, _ledger_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    from pydantic import ValidationError as PydanticValidationError

    app.add_exception_handler(PydanticValidationError, _pydantic_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
