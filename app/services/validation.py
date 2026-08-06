from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from app.config import Settings
from app.operations.base import KnownOperationError, OperationResult
from app.operations.pdf_utils import (
    count_pdf_pages,
    page_dimensions,
    render_first_page_probe,
    require_pdf_header,
)


def qpdf_check(path: Path, *, settings: Settings) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["qpdf", "--check", str(path)],
            timeout=settings.qpdf_timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "qpdf is not installed in the worker image.",
            retryable=True,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KnownOperationError(
            "QPDF_TIMEOUT",
            "qpdf validation exceeded the configured timeout.",
            details={"timeout_seconds": settings.qpdf_timeout_seconds},
            retryable=True,
        ) from exc

    if completed.returncode != 0:
        raise KnownOperationError(
            "QPDF_CHECK_FAILED",
            "PDF structural validation failed.",
            details={
                "returncode": completed.returncode,
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-4000:],
            },
        )

    return {
        "status": "passed",
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }


def validate_input_pdf(path: Path, *, settings: Settings) -> dict[str, Any]:
    require_pdf_header(path)
    page_count = count_pdf_pages(path)
    if page_count > settings.max_pages:
        raise KnownOperationError(
            "PAGE_LIMIT_EXCEEDED",
            "The PDF exceeds the configured page limit.",
            details={"page_count": page_count, "max_pages": settings.max_pages},
        )
    return {
        "mime_type": "application/pdf",
        "page_count": page_count,
        "qpdf_check": qpdf_check(path, settings=settings),
        "render_probe": render_first_page_probe(path),
    }


def validate_operation_result(
    *,
    operation: str,
    input_paths: list[Path],
    result: OperationResult,
    settings: Settings,
) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "operation": operation,
        "outputs": [],
        "assertions": {},
        "metadata": result.metadata,
    }

    input_page_counts = [count_pdf_pages(path) for path in input_paths]
    input_dimensions = [page_dimensions(path) for path in input_paths]

    for output in result.outputs:
        output_validation: dict[str, Any] = {
            "filename": output.filename,
            "mime_type": output.mime_type,
            "metadata": output.metadata,
        }
        if output.mime_type == "application/pdf":
            require_pdf_header(output.path)
            actual_pages = count_pdf_pages(output.path)
            output_validation.update(
                {
                    "page_count": actual_pages,
                    "qpdf_check": qpdf_check(output.path, settings=settings),
                    "render_probe": render_first_page_probe(output.path),
                }
            )
            if output.page_count is not None and output.page_count != actual_pages:
                raise KnownOperationError(
                    "PAGE_COUNT_MISMATCH",
                    "The output page count did not match the operation expectation.",
                    details={
                        "filename": output.filename,
                        "expected_pages": output.page_count,
                        "actual_pages": actual_pages,
                    },
                )
        else:
            output_validation["size_bytes"] = output.path.stat().st_size
        validation["outputs"].append(output_validation)

    if operation == "merge":
        expected_pages = sum(input_page_counts)
        actual_pages = validation["outputs"][0]["page_count"]
        validation["assertions"]["page_count"] = {
            "expected_pages": expected_pages,
            "actual_pages": actual_pages,
            "passed": expected_pages == actual_pages,
        }
        if expected_pages != actual_pages:
            raise KnownOperationError(
                "MERGE_PAGE_COUNT_MISMATCH",
                "Merged output does not contain the expected number of pages.",
                details={"expected_pages": expected_pages, "actual_pages": actual_pages},
            )

    if operation in {"ocr", "compress"}:
        expected_pages = input_page_counts[0]
        actual_pages = validation["outputs"][0]["page_count"]
        validation["assertions"]["page_count_unchanged"] = {
            "expected_pages": expected_pages,
            "actual_pages": actual_pages,
            "passed": expected_pages == actual_pages,
        }
        if expected_pages != actual_pages:
            raise KnownOperationError(
                "PAGE_COUNT_CHANGED",
                "The operation unexpectedly changed the page count.",
                details={"expected_pages": expected_pages, "actual_pages": actual_pages},
            )

    if operation == "compress":
        output_dimensions = page_dimensions(result.outputs[0].path)
        validation["assertions"]["dimensions_unchanged"] = {
            "passed": input_dimensions[0] == output_dimensions,
        }
        if input_dimensions[0] != output_dimensions:
            raise KnownOperationError(
                "DIMENSIONS_CHANGED",
                "Compression changed page dimensions.",
                details={
                    "input_dimensions": input_dimensions[0],
                    "output_dimensions": output_dimensions,
                },
            )

    if operation == "split":
        for index, output_validation in enumerate(validation["outputs"]):
            selected = result.outputs[index].metadata.get("selected_pages", [])
            actual_pages = output_validation["page_count"]
            if len(selected) != actual_pages:
                raise KnownOperationError(
                    "SPLIT_PAGE_COUNT_MISMATCH",
                    "Split output did not contain the requested pages.",
                    details={
                        "filename": result.outputs[index].filename,
                        "requested_pages": selected,
                        "actual_pages": actual_pages,
                    },
                )
        validation["assertions"]["selected_pages_present"] = {"passed": True}

    if operation == "extract_text":
        validation["assertions"]["json_written"] = {
            "passed": result.outputs[0].path.exists() and result.outputs[0].path.stat().st_size > 0,
        }

    return validation
