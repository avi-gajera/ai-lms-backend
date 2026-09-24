"""Domain exception hierarchy and FastAPI handlers.

Services raise `AppError` subclasses; they never know about HTTP. The handlers here map them — and
FastAPI's own request-validation / HTTP errors — to one consistent error envelope:
{"error": {"code", "message", "request_id", "details"?}}.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    """The request is valid but the resource is not in a state that allows it."""

    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class LLMError(AppError):
    """The LLM provider failed after retries (rate limit, outage, auth, unparseable output)."""

    status_code = 502
    code = "llm_error"


class LLMUnavailable(LLMError):
    """The LLM provider is not configured (e.g. missing API key)."""

    status_code = 503
    code = "llm_unavailable"


def error_envelope(code: str, message: str, details: Any | None = None) -> dict:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details is not None:
        body["details"] = details
    return {"error": body}


# Framework-raised HTTP errors (unknown route, wrong method, ...) mapped onto our error codes.
_HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log("request failed", extra={"error_code": exc.code, "error": exc.message})
        return JSONResponse(
            status_code=exc.status_code, content=error_envelope(exc.code, exc.message, exc.details)
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Same envelope as our own ValidationFailed; pydantic's per-field errors go in `details`.
        errors = [{k: v for k, v in e.items() if k != "url"} for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                ValidationFailed.code, "Request validation failed", jsonable_encoder(errors)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(_HTTP_CODES.get(exc.status_code, "http_error"), str(exc.detail)),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500, content=error_envelope("internal_error", "An unexpected error occurred.")
        )
