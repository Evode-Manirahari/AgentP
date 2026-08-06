from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.operations.base import KnownOperationError


def operation_http_error(error: KnownOperationError, *, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=error.to_dict())


def _error_content(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "retryable": retryable,
        }
    }


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("error"), dict):
        content = exc.detail
    else:
        message = exc.detail if isinstance(exc.detail, str) else "The request failed."
        details = {} if isinstance(exc.detail, str) else {"detail": exc.detail}
        content = _error_content(code="HTTP_ERROR", message=message, details=details)

    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(content),
        headers=exc.headers,
    )


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            _error_content(
                code="REQUEST_VALIDATION_FAILED",
                message="The request did not match the expected schema.",
                details={"errors": exc.errors()},
            )
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
