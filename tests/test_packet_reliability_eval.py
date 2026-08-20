"""Tests for the reliability harness itself.

A harness that scores wrongly is worse than no harness, because it produces a number people
trust. These cover the scoring maths, the ordering ground truth, and — most importantly —
that the synthetic generator really produces what it claims, since every measurement
depends on a "scanned" page genuinely having no text layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fitz")

from evals.packet_reliability import report as report_module  # noqa: E402
from evals.packet_reliability.corpus import (  # noqa: E402
    InputSpec,
    PacketCase,
    default_cases,
    load_manifest_cases,
    materialize,
)
from evals.packet_reliability.runner import CaseOutcome, SuiteResult  # noqa: E402
from evals.packet_reliability.scorers import (  # noqa: E402
    Check,
    LabelCounts,
    score_manifest,
    score_ordering,
    score_page_conservation,
    score_scanned_detection,
    score_text_recall,
    score_warnings,
    tokenize,
)


def _case(**kwargs: object) -> PacketCase:
    defaults: dict[str, object] = {
        "case_id": "example",
        "description": "example",
        "inputs": (InputSpec(filename="a.pdf", pages=2), InputSpec(filename="b.pdf", pages=3)),
    }
    defaults.update(kwargs)
    return PacketCase(**defaults)  # type: ignore[arg-type]


def _report(**overrides: object) -> dict:
    report = {
        "sequence": [1, 2],
        "inputs": [
            {"position": 1, "filename": "a.pdf", "scanned": False, "ocr_applied": False},
            {"position": 2, "filename": "b.pdf", "scanned": False, "ocr_applied": False},
        ],
        "output": {"filename": "packet.pdf", "page_count": 5},
        "summary": {"total_input_pages": 5},
        "warnings": [],
    }
    report.update(overrides)  # type: ignore[arg-type]
    return report


# --- Ground truth ------------------------------------------------------------------


def test_filename_ordering_is_case_insensitive_and_stable() -> None:
    case = _case(
        inputs=(
            InputSpec(filename="Bravo.pdf"),
            InputSpec(filename="alpha.pdf"),
            InputSpec(filename="CHARLIE.pdf"),
        ),
        order="filename",
    )

    assert case.resolved_sequence() == (2, 1, 3)


def test_as_provided_ordering_keeps_submission_order() -> None:
    assert _case().resolved_sequence() == (1, 2)


def test_an_explicit_expected_sequence_wins() -> None:
    case = _case(order="filename", expected_sequence=(2, 1))

    assert case.resolved_sequence() == (2, 1)


def test_manifest_ordering_is_semantic_and_stable_within_sections() -> None:
    case = _case(
        inputs=(
            InputSpec(filename="statement-1.pdf"),
            InputSpec(filename="identity.pdf"),
            InputSpec(filename="application.pdf"),
            InputSpec(filename="statement-2.pdf"),
        ),
        order="manifest",
        input_labels=("statement", "identity", "application", "statement"),
        manifest=(
            {"label": "application"},
            {"label": "identity"},
            {"label": "statement"},
        ),
    )

    assert case.resolved_sequence() == (3, 2, 1, 4)


def test_blank_documents_count_as_needing_ocr() -> None:
    case = _case(
        inputs=(InputSpec(filename="a.pdf"), InputSpec(filename="blank.pdf", blank=True)),
    )

    assert case.expected_scanned_positions == {2}
    assert case.requires_ocr is True


# --- Scoring -----------------------------------------------------------------------


def test_page_conservation_catches_a_dropped_page() -> None:
    check = score_page_conservation(
        _case(),
        _report(output={"filename": "packet.pdf", "page_count": 4}),
    )

    assert check.passed is False
    assert check.detail["expected_pages"] == 5
    assert check.detail["packet_pages"] == 4


def test_page_conservation_passes_when_every_page_survives() -> None:
    assert score_page_conservation(_case(), _report()).passed is True


def test_ordering_catches_a_wrong_sequence() -> None:
    check = score_ordering(_case(), _report(sequence=[2, 1]))

    assert check.passed is False
    assert check.detail == {"expected_sequence": [1, 2], "actual_sequence": [2, 1]}


def test_manifest_scoring_requires_labels_and_satisfied_sections() -> None:
    case = _case(
        order="manifest",
        input_labels=("identity", "application"),
        manifest=({"label": "application"}, {"label": "identity"}),
    )
    report = _report(
        inputs=[
            {
                "position": 1,
                "filename": "a.pdf",
                "label": "identity",
                "scanned": False,
                "ocr_applied": False,
            },
            {
                "position": 2,
                "filename": "b.pdf",
                "label": "application",
                "scanned": False,
                "ocr_applied": False,
            },
        ],
        manifest_validation=[
            {"label": "application", "satisfied": True},
            {"label": "identity", "satisfied": True},
        ],
    )

    check = score_manifest(case, report)

    assert check is not None
    assert check.passed is True


def test_manifest_scoring_catches_unsatisfied_evidence() -> None:
    case = _case(
        order="manifest",
        input_labels=("identity", "application"),
        manifest=({"label": "application"}, {"label": "identity"}),
    )
    report = _report(
        manifest_validation=[
            {"label": "application", "satisfied": False},
            {"label": "identity", "satisfied": True},
        ]
    )

    check = score_manifest(case, report)

    assert check is not None
    assert check.passed is False


def test_scanned_detection_counts_a_missed_scan_as_a_false_negative() -> None:
    case = _case(inputs=(InputSpec(filename="a.pdf"), InputSpec(filename="b.pdf", scanned=True)))

    check, counts = score_scanned_detection(case, _report())

    assert check.passed is False
    assert counts.false_negative == 1
    assert counts.true_negative == 1
    assert check.detail["misclassified"][0]["actually_scanned"] is True


def test_scanned_detection_counts_over_eager_ocr_as_a_false_positive() -> None:
    report = _report(
        inputs=[
            {"position": 1, "filename": "a.pdf", "scanned": True, "ocr_applied": True},
            {"position": 2, "filename": "b.pdf", "scanned": False, "ocr_applied": False},
        ]
    )

    check, counts = score_scanned_detection(_case(), report)

    assert check.passed is False
    assert counts.false_positive == 1


def test_label_counts_report_precision_and_recall() -> None:
    counts = LabelCounts(true_positive=3, false_positive=1, true_negative=5, false_negative=1)

    assert counts.total == 10
    assert counts.accuracy == 0.8
    assert counts.precision == 0.75
    assert counts.recall == 0.75


def test_label_counts_are_undefined_rather_than_zero_when_nothing_was_labelled() -> None:
    counts = LabelCounts()

    assert counts.accuracy is None
    assert counts.precision is None
    assert counts.recall is None


def test_text_recall_reports_the_fraction_recovered_and_what_was_lost() -> None:
    case = _case(
        inputs=(InputSpec(filename="a.pdf", tokens=("ZEPHYR", "POL-48213", "MERIDIAN")),)
    )

    check = score_text_recall(case, "the ZEPHYR policy POL-48213 was filed")

    assert check is not None
    assert check.passed is False
    assert check.detail["recall"] == pytest.approx(2 / 3)
    assert check.detail["missing_tokens"] == ["MERIDIAN"]


def test_text_recall_is_not_measured_without_known_tokens() -> None:
    assert score_text_recall(_case(), "anything") is None


def test_tokenizing_is_case_insensitive() -> None:
    assert "POL-48213" in tokenize("policy pol-48213 filed")


def test_warnings_require_the_expected_code() -> None:
    case = _case(expected_warning_codes=("LOW_TEXT_AFTER_OCR",))

    assert score_warnings(case, _report()).passed is False
    assert (
        score_warnings(case, _report(warnings=[{"code": "LOW_TEXT_AFTER_OCR"}])).passed is True
    )


# --- Reporting ---------------------------------------------------------------------


def _outcome(status: str, **kwargs: object) -> CaseOutcome:
    return CaseOutcome(
        case_id=kwargs.pop("case_id", "c"),  # type: ignore[arg-type]
        description="d",
        tags=(),
        status=status,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_taxonomy_separates_missed_scans_from_over_eager_ocr() -> None:
    detection_miss = Check(
        name="scanned_detection",
        passed=False,
        detail={
            "misclassified": [
                {"position": 1, "filename": "a", "predicted_scanned": False,
                 "actually_scanned": True},
                {"position": 2, "filename": "b", "predicted_scanned": True,
                 "actually_scanned": False},
            ]
        },
    )
    result = SuiteResult(
        outcomes=[_outcome("failed", checks=[detection_miss])],
        scanned_detection=LabelCounts(),
    )

    modes = dict(report_module.failure_taxonomy(result))

    assert modes["scanned page not detected"] == 1
    assert modes["digital page misread as scanned"] == 1


def test_the_taxonomy_records_operation_errors_by_code() -> None:
    result = SuiteResult(
        outcomes=[_outcome("errored", error_code="PDF_OPEN_FAILED", error_message="bad")],
        scanned_detection=LabelCounts(),
    )

    assert report_module.failure_taxonomy(result) == [("operation error: PDF_OPEN_FAILED", 1)]


def test_skipped_cases_are_excluded_from_the_rates_they_could_not_measure() -> None:
    result = SuiteResult(
        outcomes=[
            _outcome("passed", checks=[Check(name="output_validates", passed=True)]),
            _outcome("skipped", skip_reason="no tesseract"),
        ],
        scanned_detection=LabelCounts(),
        skipped_reason="no tesseract",
    )

    summary = report_module.summarize(result)

    assert summary["cases"]["attempted"] == 1
    assert summary["cases"]["skipped"] == 1
    assert summary["rates"]["output_validated"] == 1.0


def test_the_console_report_names_the_skip_reason() -> None:
    result = SuiteResult(
        outcomes=[_outcome("skipped", skip_reason="no tesseract")],
        scanned_detection=LabelCounts(),
        skipped_reason="no tesseract",
    )

    rendered = report_module.render_console(report_module.summarize(result))

    assert "no tesseract" in rendered


def _gate_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    result: SuiteResult,
    argv: list[str],
) -> int:
    from evals.packet_reliability import __main__ as entrypoint

    monkeypatch.setattr(entrypoint, "run_suite", lambda *args, **kwargs: result)
    return entrypoint.main(argv)


def test_the_gate_fails_when_cases_were_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped case is unmeasured, not passing.

    Skipped cases are excluded from the rate's denominator, so a run that skipped every
    scanned case still reports a perfect rate over the digital ones. Missing OCR binaries
    skip those cases automatically, which is exactly how CI would go green over zero OCR
    coverage.
    """
    result = SuiteResult(
        outcomes=[
            _outcome("passed", checks=[Check(name="output_validates", passed=True)]),
            _outcome("skipped", skip_reason="no tesseract"),
        ],
        scanned_detection=LabelCounts(),
        skipped_reason="no tesseract",
    )

    assert report_module.summarize(result)["rates"]["all_checks_passed"] == 1.0
    assert _gate_exit_code(monkeypatch, result, ["--fail-under", "0.95"]) == 1


def test_allow_skips_accepts_the_measurement_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SuiteResult(
        outcomes=[
            _outcome("passed", checks=[Check(name="output_validates", passed=True)]),
            _outcome("skipped", skip_reason="no tesseract"),
        ],
        scanned_detection=LabelCounts(),
        skipped_reason="no tesseract",
    )

    assert _gate_exit_code(monkeypatch, result, ["--fail-under", "0.95", "--allow-skips"]) == 0


def test_the_gate_fails_when_nothing_was_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SuiteResult(
        outcomes=[_outcome("skipped", skip_reason="no tesseract")],
        scanned_detection=LabelCounts(),
        skipped_reason="no tesseract",
    )

    assert report_module.summarize(result)["rates"]["all_checks_passed"] is None
    assert _gate_exit_code(monkeypatch, result, ["--fail-under", "0.95", "--allow-skips"]) == 1


def test_the_gate_passes_a_fully_measured_run(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SuiteResult(
        outcomes=[_outcome("passed", checks=[Check(name="output_validates", passed=True)])],
        scanned_detection=LabelCounts(),
    )

    assert _gate_exit_code(monkeypatch, result, ["--fail-under", "0.95"]) == 0


def test_the_gate_still_fails_a_measured_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SuiteResult(
        outcomes=[
            _outcome("passed", checks=[Check(name="output_validates", passed=True)]),
            _outcome("failed", checks=[Check(name="output_validates", passed=False)]),
        ],
        scanned_detection=LabelCounts(),
    )

    assert _gate_exit_code(monkeypatch, result, ["--fail-under", "0.95"]) == 1


def test_explicitly_skipping_ocr_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.packet_reliability import runner

    monkeypatch.setattr(runner, "missing_ocr_binaries", lambda: [])

    result = runner.run_suite(
        [_case(inputs=(InputSpec(filename="scan.pdf", scanned=True),))],
        work_root=tmp_path,
        skip_ocr_cases=True,
    )
    summary = report_module.summarize(result)

    assert summary["cases"]["skip_reason"] == "OCR cases disabled by --skip-ocr"
    assert summary["cases_detail"][0]["skip_reason"] == summary["cases"]["skip_reason"]


def test_the_markdown_report_renders_without_any_measurements() -> None:
    result = SuiteResult(outcomes=[], scanned_detection=LabelCounts())

    rendered = report_module.render_markdown(report_module.summarize(result))

    assert "prepare_packet reliability" in rendered
    assert "n/a" in rendered


# --- The generator -----------------------------------------------------------------


def test_a_generated_scanned_document_really_has_no_text_layer(tmp_path: Path) -> None:
    """Every scanned-detection number depends on this being true."""
    import fitz

    case = _case(
        case_id="gen",
        inputs=(InputSpec(filename="scan.pdf", pages=1, scanned=True, tokens=("MERIDIAN",)),),
    )

    [path] = materialize(case, tmp_path)

    with fitz.open(path) as document:
        text = "".join(page.get_text("text") for page in document)
    assert text.strip() == ""


def test_a_generated_digital_document_carries_its_tokens(tmp_path: Path) -> None:
    import fitz

    case = _case(
        case_id="gen",
        inputs=(InputSpec(filename="digital.pdf", pages=1, tokens=("MERIDIAN",)),),
    )

    [path] = materialize(case, tmp_path)

    with fitz.open(path) as document:
        text = "".join(page.get_text("text") for page in document)
    assert "MERIDIAN" in text


def test_generated_page_counts_match_the_declared_ground_truth(tmp_path: Path) -> None:
    import fitz

    case = _case(
        case_id="gen",
        inputs=(
            InputSpec(filename="a.pdf", pages=3),
            InputSpec(filename="b.pdf", pages=2, scanned=True),
        ),
    )

    paths = materialize(case, tmp_path)

    counts = []
    for path in paths:
        with fitz.open(path) as document:
            counts.append(document.page_count)
    assert counts == [3, 2]
    assert sum(counts) == case.expected_total_pages


def test_generation_is_deterministic(tmp_path: Path) -> None:
    """Same case, same pixels — otherwise a rerun's numbers are not comparable.

    Compared as rendered content rather than as file bytes: a PDF carries a creation
    timestamp and a document id that differ between two saves of identical content.
    """
    import fitz

    case = _case(
        case_id="gen",
        inputs=(InputSpec(filename="noisy.pdf", pages=1, scanned=True, noise=0.01),),
    )

    def rendered(path: Path) -> bytes:
        with fitz.open(path) as document:
            return b"".join(page.get_pixmap(dpi=72).tobytes("png") for page in document)

    [first] = materialize(case, tmp_path / "one")
    [second] = materialize(case, tmp_path / "two")

    assert rendered(first) == rendered(second)


def test_the_default_corpus_covers_both_digital_and_scanned_paths() -> None:
    cases = default_cases()

    assert len({case.case_id for case in cases}) == len(cases)
    assert any(case.requires_ocr for case in cases)
    assert any(not case.requires_ocr for case in cases)
    assert any(case.order == "manifest" for case in cases)


# --- Real-document manifests -------------------------------------------------------


def test_a_manifest_turns_real_documents_into_scoreable_cases(tmp_path: Path) -> None:
    import fitz

    document_path = tmp_path / "real.pdf"
    document = fitz.open()
    document.new_page()
    document.save(document_path)
    document.close()

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "acme-01",
                    "description": "real packet",
                    "order": "manifest",
                    "manifest": [{"label": "application", "min_count": 1}],
                    "inputs": [
                        {
                            "path": "real.pdf",
                            "label": "application",
                            "pages": 1,
                            "scanned": True,
                            "tokens": ["POL-1"],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    [case] = load_manifest_cases(tmp_path)

    assert case.case_id == "acme-01"
    assert "real" in case.tags
    assert case.expected_total_pages == 1
    assert case.expected_scanned_positions == {1}
    assert case.real_paths == (document_path.resolve(),)
    assert case.input_labels == ("application",)
    assert case.manifest == ({"label": "application", "min_count": 1},)


def test_a_manifest_pointing_at_a_missing_document_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps([{"case_id": "x", "inputs": [{"path": "gone.pdf", "pages": 1}]}]),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError):
        load_manifest_cases(tmp_path)


def test_no_manifest_means_no_real_cases(tmp_path: Path) -> None:
    assert load_manifest_cases(tmp_path) == []
