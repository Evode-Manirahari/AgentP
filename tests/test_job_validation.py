import importlib

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
jobs = importlib.import_module("app.services.jobs")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


def _document(file_id: str = "file_123") -> object:
    return models.Document(
        id=file_id,
        original_filename="source.pdf",
        mime_type="application/pdf",
        size_bytes=128,
        sha256="0" * 64,
        storage_key=f"inputs/{file_id}/source.pdf",
        page_count=2,
        status=models.DocumentStatus.VALIDATED.value,
    )


def _request(operation: str, parameters: dict[str, object]) -> object:
    return schemas.JobCreate(
        operation=operation,
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters=parameters,
    )


def test_rejects_unknown_operation_parameters() -> None:
    request = _request("compress", {"preset": "ebook", "quality": "high"})

    with pytest.raises(KnownOperationError) as exc:
        jobs._validate_job_request(request, [_document()])

    assert exc.value.code == "INVALID_PARAMETERS"
    assert exc.value.details["unsupported"] == ["quality"]


def test_rejects_invalid_compression_preset_at_create_time() -> None:
    request = _request("compress", {"preset": "archive"})

    with pytest.raises(KnownOperationError) as exc:
        jobs._validate_job_request(request, [_document()])

    assert exc.value.code == "INVALID_COMPRESSION_PRESET"
    assert exc.value.details["allowed"] == ["ebook", "print", "screen"]


@pytest.mark.parametrize("language", ["", "eng;rm", 42])
def test_rejects_invalid_ocr_language_at_create_time(language: object) -> None:
    request = _request("ocr", {"language": language})

    with pytest.raises(KnownOperationError) as exc:
        jobs._validate_job_request(request, [_document()])

    assert exc.value.code == "INVALID_OCR_LANGUAGE"


def test_accepts_valid_optional_parameters() -> None:
    request = schemas.JobCreate(
        operation="merge",
        inputs=[
            schemas.JobInputRef(file_id="file_123"),
            schemas.JobInputRef(file_id="file_456"),
        ],
        parameters={"ocr_if_needed": True, "language": "eng+fra", "deskew": False},
    )

    jobs._validate_job_request(request, [_document("file_123"), _document("file_456")])
