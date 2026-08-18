from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from app.operations.base import KnownOperationError, OperationOutput, OperationResult
from app.operations.pdf_utils import count_pdf_pages


def ocr_pdf(
    input_path: Path,
    output_path: Path,
    *,
    language: str = "eng",
    deskew: bool = True,
    skip_text: bool = True,
    timeout_seconds: int = 300,
) -> OperationResult:
    if not language.replace("+", "").replace("-", "").isalnum():
        raise KnownOperationError(
            "INVALID_OCR_LANGUAGE",
            "OCR language must contain only letters, numbers, '+', or '-'.",
            details={"language": language},
        )

    if importlib.util.find_spec("ocrmypdf") is None:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "ocrmypdf is not installed in the worker environment.",
            retryable=True,
        )

    input_pages = count_pdf_pages(input_path)
    command = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--output-type",
        "pdf",
        "--language",
        language,
        "--jobs",
        "2",
    ]
    if deskew:
        command.append("--deskew")
    if skip_text:
        command.append("--skip-text")
    command.extend([str(input_path), str(output_path)])

    try:
        completed = subprocess.run(
            command,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KnownOperationError(
            "DEPENDENCY_MISSING",
            "The Python executable used to run ocrmypdf is unavailable.",
            retryable=True,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KnownOperationError(
            "OCR_TIMEOUT",
            "OCR exceeded the configured timeout.",
            details={"timeout_seconds": timeout_seconds},
            retryable=True,
        ) from exc

    if completed.returncode != 0:
        raise KnownOperationError(
            "OCR_FAILED",
            "OCR processing failed.",
            details={
                "returncode": completed.returncode,
                "stderr": completed.stderr[-4000:],
                "stdout": completed.stdout[-2000:],
            },
            retryable=completed.returncode in {1, 2, 3},
        )

    return OperationResult(
        outputs=[
            OperationOutput(
                path=output_path,
                filename="ocr.pdf",
                page_count=input_pages,
                metadata={"input_pages": input_pages, "language": language, "deskew": deskew},
            )
        ],
        metadata={"input_pages": input_pages, "language": language, "deskew": deskew},
    )
