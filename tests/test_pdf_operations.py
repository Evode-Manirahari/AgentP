import importlib
import subprocess
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")
pytest.importorskip("pikepdf")
pytest.importorskip("pydantic_settings")

extract_text_pdf = importlib.import_module("app.operations.extract").extract_text_pdf
merge_pdfs = importlib.import_module("app.operations.merge").merge_pdfs
ocr_module = importlib.import_module("app.operations.ocr")
ocr_pdf = ocr_module.ocr_pdf
count_pdf_pages = importlib.import_module("app.operations.pdf_utils").count_pdf_pages
split_pdf = importlib.import_module("app.operations.split").split_pdf
Settings = importlib.import_module("app.config").Settings
validate_operation_result = importlib.import_module(
    "app.services.validation"
).validate_operation_result


def _write_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_merge_pdfs_preserves_page_count(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    output = tmp_path / "merged.pdf"
    _write_pdf(first, "first")
    _write_pdf(second, "second")

    result = merge_pdfs([first, second], output)

    assert output.exists()
    assert result.outputs[0].page_count == 2
    assert count_pdf_pages(output) == 2


def test_split_pdf_writes_one_output_per_range(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source, "page one")
    with fitz.open(source) as document:
        page = document.new_page()
        page.insert_text((72, 72), "page two")
        document.saveIncr()

    result = split_pdf(source, tmp_path, ["1", "2"])

    assert [output.page_count for output in result.outputs] == [1, 1]
    assert [count_pdf_pages(output.path) for output in result.outputs] == [1, 1]


def test_extract_text_pdf_writes_page_level_json(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "text.json"
    _write_pdf(source, "invoice total 42")

    result = extract_text_pdf(source, output)

    assert result.outputs[0].mime_type == "application/json"
    assert output.read_text(encoding="utf-8").find("invoice total 42") != -1


def test_extract_text_validation_checks_json_page_count(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "text.json"
    _write_pdf(source, "page one")
    with fitz.open(source) as document:
        page = document.new_page()
        page.insert_text((72, 72), "page two")
        document.saveIncr()

    result = extract_text_pdf(source, output)

    validation = validate_operation_result(
        operation="extract_text",
        input_paths=[source],
        result=result,
        settings=Settings(),
    )

    assert validation["outputs"][0]["page_count"] == 2
    assert validation["assertions"]["json_written"]["passed"] is True
    assert validation["assertions"]["json_page_count"]["passed"] is True


def test_ocr_uses_the_running_python_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "ocr.pdf"
    _write_pdf(source, "source")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output.write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(ocr_module.subprocess, "run", fake_run)

    ocr_pdf(source, output)

    assert commands[0][:3] == [sys.executable, "-m", "ocrmypdf"]


def test_ocr_reports_a_missing_python_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source, "source")
    monkeypatch.setattr(ocr_module.importlib.util, "find_spec", lambda _: None)

    with pytest.raises(importlib.import_module("app.operations.base").KnownOperationError) as exc:
        ocr_pdf(source, tmp_path / "ocr.pdf")

    assert exc.value.code == "DEPENDENCY_MISSING"
    assert "worker environment" in exc.value.message
