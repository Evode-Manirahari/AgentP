import importlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")
pytest.importorskip("pikepdf")
pytest.importorskip("pydantic_settings")

prepare_packet_module = importlib.import_module("app.operations.prepare_packet")
prepare_packet = prepare_packet_module.prepare_packet
Settings = importlib.import_module("app.config").Settings
operations_base = importlib.import_module("app.operations.base")
KnownOperationError = operations_base.KnownOperationError
OperationResult = operations_base.OperationResult
validate_operation_result = importlib.import_module(
    "app.services.validation"
).validate_operation_result

# Validating a PDF output shells out to qpdf, which CI and the app container install but a
# bare local checkout may not. The operation tests above it run everywhere.
requires_qpdf = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf is required to validate PDF outputs",
)

# is_likely_scanned() treats anything under 40 characters per page as a scan, so text
# fixtures have to be comfortably longer than that to keep OCR out of these tests.
TEXT_BODY = (
    "This onboarding document contains enough machine readable text that the packet "
    "workflow will treat it as a digital original rather than a scan."
)


def _write_pdf(path: Path, text: str, *, pages: int = 1) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"{text} Page {index + 1}.")
    document.save(path)
    document.close()


def _packet_inputs(tmp_path: Path) -> list[Path]:
    first = tmp_path / "input-1.pdf"
    second = tmp_path / "input-2.pdf"
    _write_pdf(first, TEXT_BODY, pages=2)
    _write_pdf(second, TEXT_BODY, pages=1)
    return [first, second]


def _read_report(result: object) -> dict:
    report_output = next(
        output for output in result.outputs if output.mime_type == "application/json"
    )
    return json.loads(report_output.path.read_text(encoding="utf-8"))


def test_prepare_packet_merges_every_input_page(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)

    result = prepare_packet(inputs, tmp_path, input_names=["b-second.pdf", "a-first.pdf"])

    assert [output.filename for output in result.outputs] == [
        "packet.pdf",
        "packet-audit-report.json",
    ]
    assert result.outputs[0].page_count == 3
    assert result.metadata["total_input_pages"] == 3
    assert result.metadata["ocr_applied_to_inputs"] == []


def test_prepare_packet_audit_report_records_provenance(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)

    result = prepare_packet(inputs, tmp_path, input_names=["application.pdf", "id-card.pdf"])
    report = _read_report(result)

    assert report["workflow"] == "prepare_packet"
    assert [entry["filename"] for entry in report["inputs"]] == [
        "application.pdf",
        "id-card.pdf",
    ]
    assert [entry["page_count"] for entry in report["inputs"]] == [2, 1]
    assert all(len(entry["sha256"]) == 64 for entry in report["inputs"])
    assert [step["step"] for step in report["steps"]] == [
        "inspect",
        "ocr",
        "organize",
        "merge",
    ]
    assert report["summary"] == {
        "input_count": 2,
        "total_input_pages": 3,
        "ocr_applied_count": 0,
        "warning_count": 0,
    }
    assert report["output"] == {"filename": "packet.pdf", "page_count": 3}


def test_prepare_packet_orders_by_filename_when_requested(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)

    result = prepare_packet(
        inputs,
        tmp_path,
        input_names=["02-id-card.pdf", "01-application.pdf"],
        order="filename",
    )
    report = _read_report(result)

    assert result.metadata["sequence"] == [2, 1]
    assert report["sequence"] == [2, 1]
    assert result.outputs[0].page_count == 3


def test_prepare_packet_keeps_upload_order_by_default(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)

    result = prepare_packet(inputs, tmp_path, input_names=["z-last.pdf", "a-first.pdf"])

    assert result.metadata["sequence"] == [1, 2]


def test_prepare_packet_rejects_a_single_input(tmp_path: Path) -> None:
    source = tmp_path / "only.pdf"
    _write_pdf(source, TEXT_BODY)

    with pytest.raises(KnownOperationError) as exc:
        prepare_packet([source], tmp_path)

    assert exc.value.code == "NOT_ENOUGH_INPUTS"


def test_prepare_packet_rejects_an_unknown_order(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)

    with pytest.raises(KnownOperationError) as exc:
        prepare_packet(inputs, tmp_path, order="by_vibes")

    assert exc.value.code == "INVALID_PACKET_ORDER"
    assert exc.value.details["allowed"] == ["as_provided", "filename"]


def test_prepare_packet_ocrs_scanned_inputs_and_warns_on_low_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _packet_inputs(tmp_path)
    ocr_calls: list[Path] = []

    def fake_ocr(input_path: Path, output_path: Path, **kwargs: object) -> None:
        ocr_calls.append(input_path)
        output_path.write_bytes(input_path.read_bytes())

    # The first input is a scan whose OCR yields nothing readable; the second is digital.
    monkeypatch.setattr(prepare_packet_module, "ocr_pdf", fake_ocr)
    monkeypatch.setattr(
        prepare_packet_module,
        "is_likely_scanned",
        lambda path, **kwargs: path.name != "input-2.pdf",
    )

    result = prepare_packet(inputs, tmp_path, input_names=["scan.pdf", "digital.pdf"])
    report = _read_report(result)

    assert len(ocr_calls) == 1
    assert result.metadata["ocr_applied_to_inputs"] == [1]
    assert report["inputs"][0]["ocr_applied"] is True
    assert report["inputs"][1]["ocr_applied"] is False
    assert [warning["code"] for warning in report["warnings"]] == ["LOW_TEXT_AFTER_OCR"]
    assert report["warnings"][0]["filename"] == "scan.pdf"
    assert report["summary"]["warning_count"] == 1
    # A warning describes the input, not a broken packet: every page still made it through.
    assert result.outputs[0].page_count == 3


@requires_qpdf
def test_prepare_packet_validation_checks_pages_and_report(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)
    result = prepare_packet(inputs, tmp_path, input_names=["a.pdf", "b.pdf"])

    validation = validate_operation_result(
        operation="prepare_packet",
        input_paths=inputs,
        result=result,
        settings=Settings(),
    )

    assert validation["assertions"]["packet_page_count"] == {
        "expected_pages": 3,
        "actual_pages": 3,
        "passed": True,
    }
    assert validation["assertions"]["audit_report_complete"]["passed"] is True
    assert validation["warnings"] == []


@requires_qpdf
def test_prepare_packet_validation_rejects_a_packet_missing_input_pages(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)
    result = prepare_packet(inputs, tmp_path, input_names=["a.pdf", "b.pdf"])
    # A packet that agrees with itself about its own length but silently dropped an input
    # page. The generic declared-vs-actual check cannot see this; only the input total can.
    _write_pdf(result.outputs[0].path, TEXT_BODY, pages=2)
    truncated = OperationResult(
        outputs=[replace(result.outputs[0], page_count=2), result.outputs[1]],
        metadata=result.metadata,
    )

    with pytest.raises(KnownOperationError) as exc:
        validate_operation_result(
            operation="prepare_packet",
            input_paths=inputs,
            result=truncated,
            settings=Settings(),
        )

    assert exc.value.code == "PACKET_PAGE_COUNT_MISMATCH"
    assert exc.value.details == {"expected_pages": 3, "actual_pages": 2}


@requires_qpdf
def test_prepare_packet_validation_rejects_an_invalid_audit_report(tmp_path: Path) -> None:
    inputs = _packet_inputs(tmp_path)
    result = prepare_packet(inputs, tmp_path, input_names=["a.pdf", "b.pdf"])
    result.outputs[1].path.write_text("{not json", encoding="utf-8")

    with pytest.raises(KnownOperationError) as exc:
        validate_operation_result(
            operation="prepare_packet",
            input_paths=inputs,
            result=result,
            settings=Settings(),
        )

    assert exc.value.code == "AUDIT_REPORT_JSON_INVALID"
