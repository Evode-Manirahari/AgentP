import importlib

import pytest

botocore_exceptions = pytest.importorskip("botocore.exceptions")
storage = importlib.import_module("app.services.storage")
ClientError = botocore_exceptions.ClientError


def _client_error(code: str, status_code: int) -> Exception:
    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation_name="HeadBucket",
    )


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("404", 404),
        ("NoSuchBucket", 404),
        ("NotFound", 404),
    ],
)
def test_missing_bucket_errors_are_createable(code: str, status_code: int) -> None:
    assert storage.is_missing_bucket_error(_client_error(code, status_code)) is True


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("AccessDenied", 403),
        ("InvalidAccessKeyId", 403),
        ("InternalError", 500),
    ],
)
def test_non_missing_bucket_errors_are_not_createable(code: str, status_code: int) -> None:
    assert storage.is_missing_bucket_error(_client_error(code, status_code)) is False
