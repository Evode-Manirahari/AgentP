from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.operations.base import KnownOperationError, OperationResult
from app.operations.compress import compress_pdf
from app.operations.extract import extract_text_pdf
from app.operations.merge import merge_pdfs
from app.operations.ocr import ocr_pdf
from app.operations.pdf_utils import is_likely_scanned
from app.operations.prepare_packet import prepare_packet
from app.operations.split import split_pdf

SUPPORTED_OPERATIONS = {"merge", "split", "ocr", "compress", "extract_text", "prepare_packet"}


def execute_operation(
    *,
    operation: str,
    input_paths: list[Path],
    parameters: dict[str, Any],
    workspace: Path,
    settings: Settings,
    input_names: list[str] | None = None,
) -> OperationResult:
    if operation not in SUPPORTED_OPERATIONS:
        raise KnownOperationError(
            "UNSUPPORTED_OPERATION",
            "The requested operation is not supported.",
            details={"operation": operation, "supported": sorted(SUPPORTED_OPERATIONS)},
        )

    if operation == "merge":
        merge_inputs = input_paths
        ocr_applied: list[int] = []
        if parameters.get("ocr_if_needed", False):
            merge_inputs = []
            for index, input_path in enumerate(input_paths):
                if is_likely_scanned(input_path):
                    ocr_path = workspace / f"input-{index + 1}-ocr.pdf"
                    ocr_pdf(
                        input_path,
                        ocr_path,
                        language=str(parameters.get("language", "eng")),
                        deskew=bool(parameters.get("deskew", True)),
                        timeout_seconds=settings.operation_timeout_seconds,
                    )
                    merge_inputs.append(ocr_path)
                    ocr_applied.append(index + 1)
                else:
                    merge_inputs.append(input_path)

        result = merge_pdfs(merge_inputs, workspace / "merged.pdf")
        return OperationResult(
            outputs=result.outputs,
            metadata={**result.metadata, "ocr_applied_to_inputs": ocr_applied},
        )

    if operation == "prepare_packet":
        return prepare_packet(
            input_paths,
            workspace,
            input_names=input_names,
            language=str(parameters.get("language", "eng")),
            deskew=bool(parameters.get("deskew", True)),
            order=str(parameters.get("order", "as_provided")),
            timeout_seconds=settings.operation_timeout_seconds,
        )

    if operation == "split":
        if len(input_paths) != 1:
            raise KnownOperationError(
                "INVALID_INPUT_COUNT",
                "Split requires exactly one input PDF.",
                details={"input_count": len(input_paths)},
            )
        page_ranges = parameters.get("page_ranges")
        if not isinstance(page_ranges, list) or not all(
            isinstance(item, str) for item in page_ranges
        ):
            raise KnownOperationError(
                "INVALID_PARAMETERS",
                "Split requires page_ranges as a list of strings.",
                details={"parameters": parameters},
            )
        return split_pdf(input_paths[0], workspace, page_ranges)

    if operation == "ocr":
        if len(input_paths) != 1:
            raise KnownOperationError(
                "INVALID_INPUT_COUNT",
                "OCR requires exactly one input PDF.",
                details={"input_count": len(input_paths)},
            )
        return ocr_pdf(
            input_paths[0],
            workspace / "ocr.pdf",
            language=str(parameters.get("language", "eng")),
            deskew=bool(parameters.get("deskew", True)),
            timeout_seconds=settings.operation_timeout_seconds,
        )

    if operation == "compress":
        if len(input_paths) != 1:
            raise KnownOperationError(
                "INVALID_INPUT_COUNT",
                "Compression requires exactly one input PDF.",
                details={"input_count": len(input_paths)},
            )
        return compress_pdf(
            input_paths[0],
            workspace / "compressed.pdf",
            preset=str(parameters.get("preset", "ebook")),
            timeout_seconds=settings.operation_timeout_seconds,
        )

    if operation == "extract_text":
        if len(input_paths) != 1:
            raise KnownOperationError(
                "INVALID_INPUT_COUNT",
                "Text extraction requires exactly one input PDF.",
                details={"input_count": len(input_paths)},
            )
        return extract_text_pdf(
            input_paths[0],
            workspace / "extracted-text.json",
            include_coordinates=bool(parameters.get("include_coordinates", False)),
        )

    raise AssertionError(f"Unhandled operation: {operation}")
