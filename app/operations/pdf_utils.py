from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.operations.base import KnownOperationError


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_pdf_header(path: Path) -> None:
    with path.open("rb") as file:
        header = file.read(5)
    if header != b"%PDF-":
        raise KnownOperationError(
            "UNSUPPORTED_FILE_TYPE",
            "Only PDF inputs are supported in this v0.",
            details={"path": str(path), "detected_header": header.decode("latin1", "replace")},
        )


def count_pdf_pages(path: Path) -> int:
    try:
        import fitz
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "PyMuPDF is required to inspect PDF page counts.",
            retryable=True,
        ) from exc

    try:
        with fitz.open(path) as document:
            return int(document.page_count)
    except Exception as exc:  # PyMuPDF raises several concrete types depending on corruption.
        raise KnownOperationError(
            "PDF_OPEN_FAILED",
            "The PDF could not be opened for page inspection.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def render_first_page_probe(path: Path) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "PyMuPDF is required to render validation probes.",
            retryable=True,
        ) from exc

    try:
        with fitz.open(path) as document:
            if document.page_count == 0:
                raise KnownOperationError(
                    "EMPTY_PDF",
                    "The PDF has zero pages.",
                    details={"path": str(path)},
                )
            page = document.load_page(0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
            return {
                "rendered": True,
                "page": 1,
                "width": pixmap.width,
                "height": pixmap.height,
            }
    except KnownOperationError:
        raise
    except Exception as exc:
        raise KnownOperationError(
            "PDF_RENDER_FAILED",
            "The first page could not be rendered.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def page_dimensions(path: Path) -> list[dict[str, float]]:
    try:
        import fitz
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "PyMuPDF is required to inspect PDF page dimensions.",
            retryable=True,
        ) from exc

    try:
        with fitz.open(path) as document:
            return [
                {"width": float(page.rect.width), "height": float(page.rect.height)}
                for page in document
            ]
    except Exception as exc:
        raise KnownOperationError(
            "PDF_OPEN_FAILED",
            "The PDF could not be opened for dimension inspection.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def extract_plain_text(path: Path) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "PyMuPDF is required to extract PDF text.",
            retryable=True,
        ) from exc

    try:
        with fitz.open(path) as document:
            return "\n".join(page.get_text("text") for page in document)
    except Exception as exc:
        raise KnownOperationError(
            "TEXT_EXTRACTION_FAILED",
            "Text extraction failed.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def is_likely_scanned(path: Path, *, min_chars_per_page: int = 40) -> bool:
    page_count = count_pdf_pages(path)
    if page_count == 0:
        return False
    text = extract_plain_text(path)
    return (len(text.strip()) / page_count) < min_chars_per_page

