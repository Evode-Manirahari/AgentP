"""CLI: python -m evals.packet_reliability [options]"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from evals.packet_reliability import report as report_module
from evals.packet_reliability.corpus import default_cases, load_manifest_cases
from evals.packet_reliability.runner import missing_ocr_binaries, run_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.packet_reliability",
        description="Measure prepare_packet reliability over a document corpus.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        help=(
            "Directory of real documents with a manifest.json. "
            "Real cases are added to the synthetic ones unless --real-only is given."
        ),
    )
    parser.add_argument(
        "--real-only",
        action="store_true",
        help="Score only the documents in --corpus, skipping the synthetic cases.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run only these case ids. Repeatable.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Run only cases carrying these tags. Repeatable.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write the full result, including per-case detail, as JSON.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        help="Write the report as markdown.",
    )
    parser.add_argument(
        "--keep-artifacts",
        type=Path,
        help="Keep generated documents and packets in this directory for inspection.",
    )
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help="Skip cases needing OCR even when the toolchain is present.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        metavar="RATE",
        help=(
            "Exit non-zero if the all-checks-passed rate falls below this (0-1). "
            "Use in CI once a baseline is established."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cases = [] if args.real_only else default_cases()
    if args.corpus:
        cases.extend(load_manifest_cases(args.corpus))
    if args.real_only and not args.corpus:
        print("--real-only requires --corpus", file=sys.stderr)
        return 2

    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case.case_id in wanted]
    if args.tags:
        wanted_tags = set(args.tags)
        cases = [case for case in cases if wanted_tags & set(case.tags)]

    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    missing = missing_ocr_binaries()
    if missing and not args.skip_ocr:
        print(
            f"note: OCR cases will be skipped, missing binaries: {', '.join(missing)}",
            file=sys.stderr,
        )

    if args.keep_artifacts:
        args.keep_artifacts.mkdir(parents=True, exist_ok=True)
        result = run_suite(
            cases,
            work_root=args.keep_artifacts,
            skip_ocr_cases=True if args.skip_ocr else None,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="agentp-eval-") as tmp:
            result = run_suite(
                cases,
                work_root=Path(tmp),
                skip_ocr_cases=True if args.skip_ocr else None,
            )

    summary = report_module.summarize(result)
    print(report_module.render_console(summary))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(report_module.render_markdown(summary), encoding="utf-8")
        print(f"wrote {args.markdown_out}")
    if args.keep_artifacts:
        print(f"artifacts in {args.keep_artifacts}")

    if result.errored:
        return 1
    if args.fail_under is not None:
        rate = summary["rates"]["all_checks_passed"]
        if rate is not None and rate < args.fail_under:
            print(
                f"\nall-checks-passed rate {rate:.3f} is below --fail-under {args.fail_under}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
