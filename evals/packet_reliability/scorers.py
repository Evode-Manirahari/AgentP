"""The measurements.

Each scorer answers one question a buyer would actually ask, and each is derived from what
the pipeline itself reports (the audit report and the production validator) rather than
from a reimplementation of it. A scorer that recomputed the answer would only prove the
eval agrees with itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.packet_reliability.corpus import PacketCase

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-]+")


@dataclass
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class LabelCounts:
    """Confusion counts for a binary detector, aggregated across every input document."""

    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    @property
    def total(self) -> int:
        return (
            self.true_positive + self.false_positive + self.true_negative + self.false_negative
        )

    @property
    def accuracy(self) -> float | None:
        return (self.true_positive + self.true_negative) / self.total if self.total else None

    @property
    def precision(self) -> float | None:
        predicted = self.true_positive + self.false_positive
        return self.true_positive / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positive + self.false_negative
        return self.true_positive / actual if actual else None

    def add(self, *, predicted: bool, actual: bool) -> None:
        if predicted and actual:
            self.true_positive += 1
        elif predicted and not actual:
            self.false_positive += 1
        elif not predicted and actual:
            self.false_negative += 1
        else:
            self.true_negative += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "accuracy": self.accuracy,
            "precision": self.precision,
            "recall": self.recall,
        }


def tokenize(text: str) -> set[str]:
    return {match.group(0).upper() for match in TOKEN_PATTERN.finditer(text)}


def score_page_conservation(case: PacketCase, report: dict[str, Any]) -> Check:
    """No page may be dropped or duplicated between the inputs and the packet."""
    declared_input_pages = report["summary"]["total_input_pages"]
    packet_pages = report["output"]["page_count"]
    expected = case.expected_total_pages
    return Check(
        name="page_conservation",
        passed=packet_pages == declared_input_pages == expected,
        detail={
            "expected_pages": expected,
            "declared_input_pages": declared_input_pages,
            "packet_pages": packet_pages,
        },
    )


def score_ordering(case: PacketCase, report: dict[str, Any]) -> Check:
    expected = list(case.resolved_sequence())
    actual = list(report["sequence"])
    return Check(
        name="ordering",
        passed=actual == expected,
        detail={"expected_sequence": expected, "actual_sequence": actual},
    )


def score_manifest(case: PacketCase, report: dict[str, Any]) -> Check | None:
    """Whether semantic labels and completeness evidence survived into the audit report."""
    if case.order != "manifest":
        return None

    expected_labels = list(case.input_labels)
    actual_labels = [entry.get("label") for entry in report.get("inputs", [])]
    manifest_validation = report.get("manifest_validation")
    section_labels = (
        [section.get("label") for section in manifest_validation]
        if isinstance(manifest_validation, list)
        else []
    )
    expected_sections = [section["label"] for section in case.manifest]
    every_section_satisfied = (
        isinstance(manifest_validation, list)
        and bool(manifest_validation)
        and all(
            isinstance(section, dict) and section.get("satisfied") is True
            for section in manifest_validation
        )
    )
    passed = (
        actual_labels == expected_labels
        and section_labels == expected_sections
        and every_section_satisfied
    )
    return Check(
        name="manifest_contract",
        passed=passed,
        detail={
            "expected_labels": expected_labels,
            "actual_labels": actual_labels,
            "expected_sections": expected_sections,
            "actual_sections": section_labels,
            "every_section_satisfied": every_section_satisfied,
        },
    )


def score_scanned_detection(case: PacketCase, report: dict[str, Any]) -> tuple[Check, LabelCounts]:
    """Whether the pipeline recognised which inputs had no text layer.

    This is the decision that gates OCR, so a miss here is what produces an unsearchable
    packet that still reports success.
    """
    counts = LabelCounts()
    expected_positions = case.expected_scanned_positions
    misses: list[dict[str, Any]] = []

    for entry in report["inputs"]:
        position = entry["position"]
        predicted = bool(entry["scanned"])
        actual = position in expected_positions
        counts.add(predicted=predicted, actual=actual)
        if predicted != actual:
            misses.append(
                {
                    "position": position,
                    "filename": entry["filename"],
                    "predicted_scanned": predicted,
                    "actually_scanned": actual,
                }
            )

    return (
        Check(name="scanned_detection", passed=not misses, detail={"misclassified": misses}),
        counts,
    )


def score_ocr_application(case: PacketCase, report: dict[str, Any]) -> Check:
    """OCR should run on exactly the inputs that lacked text, and no others."""
    applied = {entry["position"] for entry in report["inputs"] if entry["ocr_applied"]}
    detected = {entry["position"] for entry in report["inputs"] if entry["scanned"]}
    return Check(
        name="ocr_application",
        passed=applied == detected,
        detail={"ocr_applied_to": sorted(applied), "detected_scanned": sorted(detected)},
    )


def score_text_recall(case: PacketCase, packet_text: str) -> Check | None:
    """How much of the known content survived into the searchable packet.

    Reported per case rather than pass/fail: partial recall is the normal outcome on real
    scans, and the useful artefact is the distribution, not a threshold someone picked.
    """
    expected_by_position = case.expected_tokens()
    if not expected_by_position:
        return None

    found_tokens = tokenize(packet_text)
    expected_tokens = {
        token.upper() for tokens in expected_by_position.values() for token in tokens
    }
    if not expected_tokens:
        return None

    recovered = {token for token in expected_tokens if token in found_tokens}
    missing = sorted(expected_tokens - recovered)
    recall = len(recovered) / len(expected_tokens)
    return Check(
        name="text_recall",
        passed=not missing,
        detail={
            "recall": recall,
            "expected_token_count": len(expected_tokens),
            "recovered_token_count": len(recovered),
            "missing_tokens": missing,
        },
    )


def score_warnings(case: PacketCase, report: dict[str, Any]) -> Check:
    expected = set(case.expected_warning_codes)
    actual = {warning["code"] for warning in report.get("warnings", [])}
    return Check(
        name="warnings",
        passed=expected.issubset(actual),
        detail={"expected": sorted(expected), "actual": sorted(actual)},
    )


def score_silent_loss(report: dict[str, Any], recall_check: Check) -> Check:
    """The failure a customer discovers weeks later: content lost, nothing flagged.

    A packet that loses text and says so is a known limitation. A packet that loses text
    and reports success is the one that breaks the product's central promise, so it is
    scored separately from recall itself.
    """
    recall = recall_check.detail.get("recall", 1.0)
    warnings = report.get("warnings", [])
    silent = recall < 1.0 and not warnings
    return Check(
        name="no_silent_loss",
        passed=not silent,
        detail={
            "recall": recall,
            "warning_codes": [warning["code"] for warning in warnings],
            "missing_tokens": recall_check.detail.get("missing_tokens", []),
        },
    )


def score_validation(validation: dict[str, Any] | None, error: str | None) -> Check:
    """Did the packet pass the same validator the production worker runs before success."""
    return Check(
        name="output_validates",
        passed=validation is not None and error is None,
        detail={"error": error} if error else {"assertions": (validation or {}).get("assertions")},
    )


def extract_packet_text(packet_path: Path) -> str:
    from app.operations.pdf_utils import extract_plain_text

    return extract_plain_text(packet_path)
