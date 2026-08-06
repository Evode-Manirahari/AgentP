from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.operations.base import KnownOperationError, OperationOutput, OperationResult


def extract_text_pdf(
    input_path: Path,
    output_path: Path,
    *,
    include_coordinates: bool = False,
) -> OperationResult:
    try:
        import fitz
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "PyMuPDF is required to extract PDF text.",
            retryable=True,
        ) from exc

    pages: list[dict[str, Any]] = []
    try:
        with fitz.open(input_path) as document:
            for index, page in enumerate(document, start=1):
                item: dict[str, Any] = {
                    "page": index,
                    "text": page.get_text("text"),
                }
                if include_coordinates:
                    item["blocks"] = [
                        {
                            "bbox": [float(value) for value in block[:4]],
                            "text": block[4],
                            "block_no": int(block[5]),
                            "block_type": int(block[6]),
                        }
                        for block in page.get_text("blocks")
                    ]
                pages.append(item)
    except Exception as exc:
        raise KnownOperationError(
            "TEXT_EXTRACTION_FAILED",
            "Text extraction failed.",
            details={"reason": str(exc)},
        ) from exc

    payload = {
        "source": input_path.name,
        "page_count": len(pages),
        "pages": pages,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return OperationResult(
        outputs=[
            OperationOutput(
                path=output_path,
                filename="extracted-text.json",
                mime_type="application/json",
                page_count=len(pages),
                metadata={"page_count": len(pages), "include_coordinates": include_coordinates},
            )
        ],
        metadata={"page_count": len(pages), "include_coordinates": include_coordinates},
    )

