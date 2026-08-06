from app.operations.base import KnownOperationError


def test_known_operation_error_envelope() -> None:
    error = KnownOperationError(
        "INVALID_PAGE_RANGE",
        "Page 17 does not exist.",
        details={"page_count": 12, "requested_range": "10-17"},
    )

    assert error.to_dict() == {
        "error": {
            "code": "INVALID_PAGE_RANGE",
            "message": "Page 17 does not exist.",
            "details": {"page_count": 12, "requested_range": "10-17"},
            "retryable": False,
        }
    }

