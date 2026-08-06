import importlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic_settings")
pytest.importorskip("sqlalchemy")

api_files = importlib.import_module("app.api.files")
config = importlib.import_module("app.config")
models = importlib.import_module("app.models")


class FakeSession:
    def __init__(self, document: object | None) -> None:
        self.document = document

    def get(self, model: object, file_id: str) -> object | None:
        return self.document


class FakeStorageService:
    def __init__(self, settings: object) -> None:
        self.settings = settings

    def download_to_path(self, *, key: str, path: Path) -> None:
        path.write_bytes(b"%PDF-1.7\n%AgentP\n")


def _document() -> Any:
    return models.Document(
        id="file_123",
        original_filename="merged.pdf",
        mime_type="application/pdf",
        size_bytes=17,
        sha256="a" * 64,
        storage_key="outputs/job_123/merged.pdf",
        status=models.DocumentStatus.VALIDATED.value,
    )


def test_download_file_content_returns_pdf_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_files, "StorageService", FakeStorageService)

    response = api_files.download_file_content(
        "file_123",
        FakeSession(_document()),
        config.Settings(),
    )

    response_path = Path(response.path)
    try:
        assert response.media_type == "application/pdf"
        assert response_path.read_bytes() == b"%PDF-1.7\n%AgentP\n"
        assert 'filename="merged.pdf"' in response.headers["content-disposition"]
    finally:
        response_path.unlink(missing_ok=True)


def test_download_file_content_returns_not_found_for_missing_file() -> None:
    with pytest.raises(api_files.HTTPException) as exc:
        api_files.download_file_content("missing", FakeSession(None), config.Settings())

    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "FILE_NOT_FOUND"
