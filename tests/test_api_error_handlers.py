import asyncio
import importlib
import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
fastapi_exceptions = pytest.importorskip("fastapi.exceptions")
api_errors = importlib.import_module("app.api.errors")

HTTPException = fastapi.HTTPException
RequestValidationError = fastapi_exceptions.RequestValidationError
http_exception_handler = api_errors.http_exception_handler
request_validation_exception_handler = api_errors.request_validation_exception_handler
status = fastapi.status


def _json_body(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def test_http_exception_handler_preserves_error_envelope() -> None:
    response = asyncio.run(
        http_exception_handler(
            None,  # type: ignore[arg-type]
            HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "JOB_NOT_FOUND",
                        "message": "The requested job does not exist.",
                        "details": {"job_id": "job_missing"},
                        "retryable": False,
                    }
                },
            ),
        )
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _json_body(response) == {
        "error": {
            "code": "JOB_NOT_FOUND",
            "message": "The requested job does not exist.",
            "details": {"job_id": "job_missing"},
            "retryable": False,
        }
    }


def test_request_validation_handler_returns_error_envelope() -> None:
    response = asyncio.run(
        request_validation_exception_handler(
            None,  # type: ignore[arg-type]
            RequestValidationError(
                [
                    {
                        "loc": ("body", "operation"),
                        "msg": "Field required",
                        "type": "missing",
                    }
                ]
            ),
        )
    )

    body = _json_body(response)

    assert response.status_code == 422
    assert body["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"]["errors"][0]["loc"] == ["body", "operation"]
