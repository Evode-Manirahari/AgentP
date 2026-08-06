from __future__ import annotations

from pathlib import Path

from app.operations.base import KnownOperationError, OperationOutput, OperationResult
from app.operations.pdf_utils import count_pdf_pages


def merge_pdfs(input_paths: list[Path], output_path: Path) -> OperationResult:
    if len(input_paths) < 2:
        raise KnownOperationError(
            "NOT_ENOUGH_INPUTS",
            "Merge requires at least two PDF inputs.",
            details={"input_count": len(input_paths)},
        )

    try:
        import pikepdf
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "pikepdf is required to merge PDFs.",
            retryable=True,
        ) from exc

    expected_pages = sum(count_pdf_pages(path) for path in input_paths)

    try:
        output_pdf = pikepdf.Pdf.new()
        for input_path in input_paths:
            with pikepdf.open(input_path) as input_pdf:
                output_pdf.pages.extend(input_pdf.pages)
        output_pdf.save(output_path)
        output_pdf.close()
    except Exception as exc:
        raise KnownOperationError(
            "MERGE_FAILED",
            "PDF merge failed.",
            details={"reason": str(exc)},
        ) from exc

    return OperationResult(
        outputs=[
            OperationOutput(
                path=output_path,
                filename="merged.pdf",
                page_count=expected_pages,
                metadata={"expected_pages": expected_pages},
            )
        ],
        metadata={"expected_pages": expected_pages},
    )

