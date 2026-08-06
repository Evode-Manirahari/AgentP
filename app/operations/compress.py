from __future__ import annotations

import subprocess
from pathlib import Path

from app.operations.base import KnownOperationError, OperationOutput, OperationResult
from app.operations.pdf_utils import count_pdf_pages

PRESETS = {
    "screen": "/screen",
    "ebook": "/ebook",
    "print": "/printer",
}


def compress_pdf(
    input_path: Path,
    output_path: Path,
    *,
    preset: str = "ebook",
    timeout_seconds: int = 300,
) -> OperationResult:
    if preset not in PRESETS:
        raise KnownOperationError(
            "INVALID_COMPRESSION_PRESET",
            "Unsupported compression preset.",
            details={"preset": preset, "allowed": sorted(PRESETS)},
        )

    input_pages = count_pdf_pages(input_path)
    before_bytes = input_path.stat().st_size
    command = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-dPDFSETTINGS={PRESETS[preset]}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={output_path}",
        str(input_path),
    ]

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
            "Ghostscript is not installed in the worker image.",
            retryable=True,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise KnownOperationError(
            "COMPRESSION_TIMEOUT",
            "Compression exceeded the configured timeout.",
            details={"timeout_seconds": timeout_seconds},
            retryable=True,
        ) from exc

    if completed.returncode != 0 or not output_path.exists():
        raise KnownOperationError(
            "COMPRESSION_FAILED",
            "PDF compression failed.",
            details={
                "returncode": completed.returncode,
                "stderr": completed.stderr[-4000:],
                "stdout": completed.stdout[-2000:],
            },
        )

    after_bytes = output_path.stat().st_size
    reduction = ((before_bytes - after_bytes) / before_bytes * 100) if before_bytes else 0
    metadata = {
        "preset": preset,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "reduction_percent": round(reduction, 2),
        "input_pages": input_pages,
    }
    return OperationResult(
        outputs=[
            OperationOutput(
                path=output_path,
                filename="compressed.pdf",
                page_count=input_pages,
                metadata=metadata,
            )
        ],
        metadata=metadata,
    )

