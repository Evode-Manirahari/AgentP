"""Turns suite results into the numbers and the failure taxonomy."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from evals.packet_reliability.runner import SuiteResult


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def failure_taxonomy(result: SuiteResult) -> list[tuple[str, int]]:
    """What actually went wrong, most common first.

    Counts failure modes rather than cases, because one packet can fail two ways and both
    are worth fixing.
    """
    counter: Counter[str] = Counter()
    for outcome in result.outcomes:
        if outcome.status == "errored":
            counter[f"operation error: {outcome.error_code}"] += 1
            continue
        for check in outcome.failed_checks:
            if check.name == "scanned_detection":
                for miss in check.detail.get("misclassified", []):
                    direction = (
                        "scanned page not detected"
                        if miss["actually_scanned"]
                        else "digital page misread as scanned"
                    )
                    counter[direction] += 1
            elif check.name == "text_recall":
                counter["text lost through OCR"] += 1
            else:
                counter[check.name.replace("_", " ") + " failed"] += 1
    return counter.most_common()


def summarize(result: SuiteResult) -> dict[str, Any]:
    attempted = result.attempted
    recalls = [
        outcome.text_recall for outcome in attempted if outcome.text_recall is not None
    ]
    def check_rate(name: str) -> float | None:
        """Score a check only over cases where it applies.

        A case that was correctly refused never produced a packet, so it has no ordering
        to be wrong about. Counting it as a miss would understate reliability and, worse,
        make correct refusals look like defects.
        """
        applicable = [
            outcome
            for outcome in attempted
            if any(check.name == name for check in outcome.checks)
        ]
        passing = [
            outcome
            for outcome in applicable
            if any(check.name == name and check.passed for check in outcome.checks)
        ]
        return _rate(len(passing), len(applicable))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases": {
            "total": len(result.outcomes),
            "attempted": len(attempted),
            "passed": len(result.passed),
            "failed": len(result.failed),
            "errored": len(result.errored),
            "skipped": len(result.skipped),
            "skip_reason": result.skipped_reason,
        },
        "rates": {
            "packet_produced": _rate(
                len(attempted) - len(result.errored), len(attempted)
            ),
            "output_validated": check_rate("output_validates"),
            "ordering_correct": check_rate("ordering"),
            "pages_conserved": check_rate("page_conservation"),
            "all_checks_passed": _rate(len(result.passed), len(attempted)),
            "loss_reported": check_rate("no_silent_loss"),
        },
        "text_recall": {
            "cases_measured": len(recalls),
            "mean": statistics.fmean(recalls) if recalls else None,
            "median": statistics.median(recalls) if recalls else None,
            "min": min(recalls) if recalls else None,
        },
        "scanned_detection": result.scanned_detection.as_dict(),
        "failure_taxonomy": [
            {"mode": mode, "count": count} for mode, count in failure_taxonomy(result)
        ],
        "cases_detail": [outcome.as_dict() for outcome in result.outcomes],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    cases = summary["cases"]
    rates = summary["rates"]
    recall = summary["text_recall"]
    detection = summary["scanned_detection"]
    documents_labelled = sum(
        detection[name]
        for name in ("true_positive", "false_positive", "true_negative", "false_negative")
    )

    lines = [
        "# prepare_packet reliability",
        "",
        f"Generated {summary['generated_at']}.",
        "",
        "## Headline",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Cases attempted | {cases['attempted']} of {cases['total']} |",
        f"| Packet produced | {_percent(rates['packet_produced'])} |",
        f"| Output validated | {_percent(rates['output_validated'])} |",
        f"| Ordering correct | {_percent(rates['ordering_correct'])} |",
        f"| Pages conserved | {_percent(rates['pages_conserved'])} |",
        f"| All checks passed | {_percent(rates['all_checks_passed'])} |",
        f"| Text loss reported, never silent | {_percent(rates['loss_reported'])} |",
        f"| Text recall (mean) | {_number(recall['mean'])} |",
        f"| Text recall (worst case) | {_number(recall['min'])} |",
        "",
        "## Scanned-page detection",
        "",
        "The decision that gates OCR. A miss produces an unsearchable packet that still",
        "reports success, so recall matters more than accuracy here.",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Documents labelled | {documents_labelled} |",
        f"| Accuracy | {_percent(detection['accuracy'])} |",
        f"| Precision | {_percent(detection['precision'])} |",
        f"| Recall | {_percent(detection['recall'])} |",
        f"| Missed scans (false negative) | {detection['false_negative']} |",
        f"| Over-eager OCR (false positive) | {detection['false_positive']} |",
        "",
    ]

    if cases["skipped"]:
        lines.extend(
            [
                "## Skipped",
                "",
                f"{cases['skipped']} case(s) skipped: {cases['skip_reason']}.",
                "OCR-dependent numbers above are not complete until these run.",
                "",
            ]
        )

    taxonomy = summary["failure_taxonomy"]
    lines.extend(["## Failure taxonomy", ""])
    if not taxonomy:
        lines.extend(["No failures.", ""])
    else:
        total = sum(item["count"] for item in taxonomy)
        lines.extend(["| Failure mode | Count | Share |", "| --- | --- | --- |"])
        for item in taxonomy:
            share = item["count"] / total
            lines.append(f"| {item['mode']} | {item['count']} | {share * 100:.0f}% |")
        lines.append("")

    lines.extend(
        ["## Cases", "", "| Case | Status | Recall | Notes |", "| --- | --- | --- | --- |"]
    )
    for case in summary["cases_detail"]:
        if case["status"] == "skipped":
            note = case["skip_reason"] or ""
        elif case["status"] == "errored":
            note = f"{case['error_code']}: {case['error_message']}"
        else:
            failed = [check["name"] for check in case["checks"] if not check["passed"]]
            note = ", ".join(failed) if failed else ""
        recall_value = case["text_recall"]
        lines.append(
            f"| `{case['case_id']}` | {case['status']} | "
            f"{'' if recall_value is None else f'{recall_value:.2f}'} | {note} |"
        )
    lines.append("")

    return "\n".join(lines)


def render_console(summary: dict[str, Any]) -> str:
    cases = summary["cases"]
    rates = summary["rates"]
    recall = summary["text_recall"]
    detection = summary["scanned_detection"]

    lines = [
        f"packets: {cases['attempted']}   "
        f"validated: {_percent(rates['output_validated'])}   "
        f"all checks: {_percent(rates['all_checks_passed'])}",
        f"ordering correct: {_percent(rates['ordering_correct'])}   "
        f"pages conserved: {_percent(rates['pages_conserved'])}",
        f"scanned detection: accuracy {_percent(detection['accuracy'])}, "
        f"recall {_percent(detection['recall'])}, "
        f"missed {detection['false_negative']}",
        f"text recall: mean {_number(recall['mean'])}, worst {_number(recall['min'])} "
        f"({recall['cases_measured']} cases)",
        f"text loss reported (never silent): {_percent(rates['loss_reported'])}",
    ]
    if cases["skipped"]:
        lines.append(f"skipped: {cases['skipped']} ({cases['skip_reason']})")

    taxonomy = summary["failure_taxonomy"]
    if taxonomy:
        total = sum(item["count"] for item in taxonomy)
        lines.append("top failures:")
        for item in taxonomy[:5]:
            share = item["count"] / total
            lines.append(f"  {share * 100:>3.0f}%  {item['mode']} ({item['count']})")
    else:
        lines.append("no failures")

    return "\n".join(lines)
