from __future__ import annotations

from pathlib import Path

from app.operations.base import KnownOperationError, OperationOutput, OperationResult
from app.operations.pdf_utils import count_pdf_pages


def _parse_page_bound(
    raw_value: str,
    *,
    default: int,
    page_count: int,
    raw_range: str,
    invalid_part: str,
) -> int:
    value = raw_value.strip()
    if not value:
        return default
    if not value.isdigit():
        raise KnownOperationError(
            "INVALID_PAGE_RANGE",
            "A requested page range contains a non-numeric page number.",
            details={
                "page_count": page_count,
                "requested_range": raw_range,
                "invalid_part": invalid_part,
            },
        )
    return int(value)


def parse_page_ranges(page_ranges: list[str], page_count: int) -> list[list[int]]:
    if not page_ranges:
        raise KnownOperationError("MISSING_PAGE_RANGE", "At least one page range is required.")

    parsed: list[list[int]] = []
    for raw_range in page_ranges:
        pages: list[int] = []
        for raw_part in raw_range.split(","):
            part = raw_part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = [piece.strip() for piece in part.split("-", 1)]
                start = _parse_page_bound(
                    start_text,
                    default=1,
                    page_count=page_count,
                    raw_range=raw_range,
                    invalid_part=part,
                )
                end = _parse_page_bound(
                    end_text,
                    default=page_count,
                    page_count=page_count,
                    raw_range=raw_range,
                    invalid_part=part,
                )
            else:
                start = end = _parse_page_bound(
                    part,
                    default=1,
                    page_count=page_count,
                    raw_range=raw_range,
                    invalid_part=part,
                )

            if start < 1 or end < 1 or start > end or end > page_count:
                raise KnownOperationError(
                    "INVALID_PAGE_RANGE",
                    "A requested page range is outside the document.",
                    details={
                        "page_count": page_count,
                        "requested_range": raw_range,
                        "invalid_part": part,
                    },
                )
            pages.extend(range(start - 1, end))

        if not pages:
            raise KnownOperationError(
                "INVALID_PAGE_RANGE",
                "A page range did not contain any pages.",
                details={"requested_range": raw_range},
            )
        parsed.append(pages)

    return parsed


def split_pdf(input_path: Path, output_dir: Path, page_ranges: list[str]) -> OperationResult:
    try:
        import pikepdf
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "pikepdf is required to split PDFs.",
            retryable=True,
        ) from exc

    total_pages = count_pdf_pages(input_path)
    parsed_ranges = parse_page_ranges(page_ranges, total_pages)
    outputs: list[OperationOutput] = []

    try:
        with pikepdf.open(input_path) as source_pdf:
            for index, selected_pages in enumerate(parsed_ranges, start=1):
                output_pdf = pikepdf.Pdf.new()
                for page_index in selected_pages:
                    output_pdf.pages.append(source_pdf.pages[page_index])
                output_path = output_dir / f"split-{index:03d}.pdf"
                output_pdf.save(output_path)
                output_pdf.close()
                outputs.append(
                    OperationOutput(
                        path=output_path,
                        filename=output_path.name,
                        page_count=len(selected_pages),
                        metadata={
                            "requested_range": page_ranges[index - 1],
                            "selected_pages": [page + 1 for page in selected_pages],
                        },
                    )
                )
    except KnownOperationError:
        raise
    except Exception as exc:
        raise KnownOperationError(
            "SPLIT_FAILED",
            "PDF split failed.",
            details={"reason": str(exc)},
        ) from exc

    return OperationResult(
        outputs=outputs,
        metadata={"input_pages": total_pages, "ranges": page_ranges},
    )
