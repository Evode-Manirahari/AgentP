"""Drives prepare_packet over the corpus and collects per-case outcomes.

Runs the operation in process rather than over HTTP. What is under measurement is document
handling, not API plumbing, and the in-process path needs no Postgres, Redis, or object
storage — so this stays runnable on a laptop and in CI.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.packet_reliability import scorers
from evals.packet_reliability.corpus import PacketCase, materialize
from evals.packet_reliability.scorers import Check, LabelCounts

OCR_BINARIES = ("tesseract", "gs")


def missing_ocr_binaries() -> list[str]:
    """ocrmypdf shells out to these. Without them, OCR cases cannot be judged."""
    return [name for name in OCR_BINARIES if shutil.which(name) is None]


@dataclass
class CaseOutcome:
    case_id: str
    description: str
    tags: tuple[str, ...]
    status: str
    checks: list[Check] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    skip_reason: str | None = None
    duration_seconds: float = 0.0
    text_recall: float | None = None

    @property
    def failed_checks(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "tags": list(self.tags),
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "skip_reason": self.skip_reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "text_recall": self.text_recall,
            "checks": [
                {"name": check.name, "passed": check.passed, "detail": check.detail}
                for check in self.checks
            ],
        }


@dataclass
class SuiteResult:
    outcomes: list[CaseOutcome]
    scanned_detection: LabelCounts
    skipped_reason: str | None = None

    @property
    def attempted(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.status != "skipped"]

    @property
    def passed(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.status == "passed"]

    @property
    def failed(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.status == "failed"]

    @property
    def errored(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.status == "errored"]

    @property
    def skipped(self) -> list[CaseOutcome]:
        return [item for item in self.outcomes if item.status == "skipped"]


def _run_case(case: PacketCase, workspace: Path) -> tuple[CaseOutcome, LabelCounts]:
    from app.config import get_settings
    from app.operations.base import KnownOperationError
    from app.operations.prepare_packet import AUDIT_REPORT_FILENAME, prepare_packet
    from app.services.validation import validate_operation_result

    outcome = CaseOutcome(
        case_id=case.case_id,
        description=case.description,
        tags=case.tags,
        status="passed",
    )

    inputs_dir = workspace / "inputs"
    output_dir = workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        input_paths = materialize(case, inputs_dir)
        result = prepare_packet(
            input_paths,
            output_dir,
            input_names=[spec.filename for spec in case.inputs],
            order=case.order,
            input_labels=list(case.input_labels) if case.input_labels else None,
            manifest=[dict(section) for section in case.manifest] if case.manifest else None,
            allow_unlisted=case.allow_unlisted,
        )
    except KnownOperationError as exc:
        outcome.error_code = exc.code
        outcome.error_message = exc.message
        outcome.duration_seconds = time.monotonic() - started
        if case.expected_error_code:
            # Refusing an input it cannot process is a correct outcome, not a failure.
            matched = exc.code == case.expected_error_code
            outcome.status = "passed" if matched else "failed"
            outcome.checks.append(
                Check(
                    name="expected_refusal",
                    passed=matched,
                    detail={"expected": case.expected_error_code, "actual": exc.code},
                )
            )
            return outcome, LabelCounts()
        outcome.status = "errored"
        return outcome, LabelCounts()
    except Exception as exc:  # An unexpected crash is itself a reliability finding.
        outcome.status = "errored"
        outcome.error_code = "UNHANDLED_EXCEPTION"
        outcome.error_message = f"{type(exc).__name__}: {exc}"
        outcome.duration_seconds = time.monotonic() - started
        return outcome, LabelCounts()

    validation: dict[str, Any] | None = None
    validation_error: str | None = None
    try:
        validation = validate_operation_result(
            operation="prepare_packet",
            input_paths=input_paths,
            result=result,
            settings=get_settings(),
        )
    except KnownOperationError as exc:
        validation_error = f"{exc.code}: {exc.message}"
    except Exception as exc:
        validation_error = f"{type(exc).__name__}: {exc}"

    report_path = output_dir / AUDIT_REPORT_FILENAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    packet_path = next(
        output.path for output in result.outputs if output.mime_type == "application/pdf"
    )

    outcome.checks.append(scorers.score_page_conservation(case, report))
    outcome.checks.append(scorers.score_ordering(case, report))
    manifest_check = scorers.score_manifest(case, report)
    if manifest_check is not None:
        outcome.checks.append(manifest_check)
    detection_check, counts = scorers.score_scanned_detection(case, report)
    outcome.checks.append(detection_check)
    outcome.checks.append(scorers.score_ocr_application(case, report))
    outcome.checks.append(scorers.score_warnings(case, report))
    outcome.checks.append(scorers.score_validation(validation, validation_error))

    recall_check = scorers.score_text_recall(case, scorers.extract_packet_text(packet_path))
    if recall_check is not None:
        outcome.text_recall = recall_check.detail["recall"]
        if case.tolerate_text_loss:
            # Text loss is expected here; what is being judged is whether it was reported.
            recall_check.passed = True
            recall_check.detail["tolerated"] = True
        outcome.checks.append(recall_check)
        outcome.checks.append(scorers.score_silent_loss(report, recall_check))

    outcome.duration_seconds = time.monotonic() - started
    outcome.status = "passed" if not outcome.failed_checks else "failed"
    return outcome, counts


def run_suite(
    cases: list[PacketCase],
    *,
    work_root: Path,
    skip_ocr_cases: bool | None = None,
) -> SuiteResult:
    missing = missing_ocr_binaries()
    if skip_ocr_cases is None:
        skip_ocr_cases = bool(missing)
    skip_reason = None
    if skip_ocr_cases:
        skip_reason = (
            f"OCR toolchain unavailable (missing: {', '.join(missing)})"
            if missing
            else "OCR cases disabled by --skip-ocr"
        )

    outcomes: list[CaseOutcome] = []
    aggregate = LabelCounts()

    for case in cases:
        if case.requires_ocr and skip_ocr_cases:
            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    description=case.description,
                    tags=case.tags,
                    status="skipped",
                    skip_reason=skip_reason,
                )
            )
            continue

        case_dir = work_root / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        outcome, counts = _run_case(case, case_dir)
        aggregate.true_positive += counts.true_positive
        aggregate.false_positive += counts.false_positive
        aggregate.true_negative += counts.true_negative
        aggregate.false_negative += counts.false_negative
        outcomes.append(outcome)

    return SuiteResult(
        outcomes=outcomes,
        scanned_detection=aggregate,
        skipped_reason=skip_reason,
    )
