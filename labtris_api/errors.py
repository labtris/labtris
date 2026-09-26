from __future__ import annotations

from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


def bad_request(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("bad_request", message, 400, detail)


def not_found(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("not_found", message, 404, detail)


def conflict(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("conflict", message, 409, detail)


def unprocessable(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("unprocessable", message, 422, detail)


def runtime_error(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("runtime_error", message, 502, detail)


def internal(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    return ApiError("internal", message, 500, detail)


def unsupported(message: str, detail: dict[str, Any] | None = None) -> ApiError:
    """This build can never do it — not "not right now", and not the caller's
    fault, so neither 400 nor 409 fits. Used for the features that need to
    reconfigure the host's own network stack, which a containerised Labtris
    has no access to."""
    return ApiError("unsupported_deployment", message, 501, detail)


def envelope(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=envelope(exc.code, exc.message, exc.detail))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A bug still has to answer in the API's own language.

    Unregistered, this left Starlette to return the plain string "Internal
    Server Error", so the UI — which parses every response as JSON — reported
    `Unexpected token 'I'` and the real failure never reached anyone. The
    traceback goes to the log; the type and message go to the caller, because
    this is a single-tenant tool someone runs on their own machine and "an
    error occurred" wastes their afternoon."""
    logger.exception(
        "unhandled",
        path=request.url.path,
        method=request.method,
        error=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content=envelope(
            "internal",
            f"{type(exc).__name__}: {exc}".strip()[:400] or "internal error",
            {"path": request.url.path},
        ),
    )
