from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class KnownOperationError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            }
        }


@dataclass(frozen=True)
class OperationOutput:
    path: Path
    filename: str
    mime_type: str = "application/pdf"
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationResult:
    outputs: list[OperationOutput]
    metadata: dict[str, Any] = field(default_factory=dict)

