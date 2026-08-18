from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from agentp_client.client import AgentPClient, AgentPClientError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentp",
        description="Run AgentP document workflows from one command.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    packet = subparsers.add_parser(
        "packet",
        help="Upload PDFs, prepare a validated packet, and download both artifacts.",
    )
    packet.add_argument("documents", nargs="+", type=Path, metavar="PDF")
    packet.add_argument(
        "--label",
        action="append",
        dest="labels",
        help="Semantic label for the corresponding PDF; repeat once per PDF.",
    )
    packet.add_argument(
        "--manifest",
        type=Path,
        help="JSON array of ordered section definitions. Implies --order manifest.",
    )
    packet.add_argument(
        "--order",
        choices=["as_provided", "filename", "manifest"],
        help="Document ordering; defaults to manifest when --manifest is set.",
    )
    packet.add_argument("--allow-unlisted", action="store_true")
    packet.add_argument("--language", default="eng", help="OCRmyPDF language code.")
    packet.add_argument("--no-deskew", action="store_true", help="Disable OCR deskewing.")
    packet.add_argument("--out", type=Path, default=Path("agentp-output"))
    packet.add_argument("--overwrite", action="store_true")
    packet.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS")
    packet.add_argument("--poll-interval", type=float, default=1.0, metavar="SECONDS")
    packet.add_argument("--idempotency-key")
    packet.add_argument(
        "--api-url",
        help="AgentP base URL; defaults to AGENTP_API_URL or http://localhost:8000.",
    )
    packet.add_argument(
        "--api-key",
        help="API key; prefer AGENTP_API_KEY so the secret stays out of shell history.",
    )
    packet.add_argument("--json", action="store_true", dest="json_output")
    packet.add_argument("--quiet", action="store_true")
    packet.set_defaults(handler=_run_packet)
    return parser


def _read_manifest(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentPClientError(
            "MANIFEST_READ_FAILED",
            "Could not read the manifest file.",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentPClientError(
            "MANIFEST_JSON_INVALID",
            "The manifest file is not valid JSON.",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise AgentPClientError(
            "MANIFEST_SCHEMA_INVALID",
            "The manifest file must contain a JSON array of section objects.",
            details={"path": str(path)},
        )
    return payload


def _suggested_fix(error: AgentPClientError) -> str:
    fixes = {
        "API_KEY_MISSING": "Set AGENTP_API_KEY to an active key for the target workspace.",
        "NETWORK_ERROR": "Check --api-url and confirm AgentP is ready with GET /ready.",
        "UNAUTHORIZED": "Set AGENTP_API_KEY to an active key for the target workspace.",
        "REQUEST_VALIDATION_FAILED": "Check labels and manifest fields, then rerun with --help.",
        "INVALID_PACKET_LABELS": "Repeat --label exactly once per positional PDF, in order.",
        "INVALID_PACKET_MANIFEST": "Pass --manifest plus one --label per PDF.",
        "PACKET_LABEL_NOT_IN_MANIFEST": (
            "Add the missing label to the manifest or pass --allow-unlisted."
        ),
        "PACKET_MANIFEST_COUNT_MISMATCH": (
            "Adjust the input labels or the section min_count/max_count constraints."
        ),
        "WORKSPACE_STORAGE_LIMIT_EXCEEDED": (
            "Delete unneeded files, shorten retention, or raise the workspace storage limit."
        ),
        "WORKSPACE_DOCUMENT_LIMIT_EXCEEDED": (
            "Delete unneeded files or raise the workspace document limit."
        ),
        "WORKSPACE_ACTIVE_JOB_LIMIT_EXCEEDED": (
            "Wait for active jobs to finish or cancel a queued job, then retry."
        ),
        "WORKSPACE_JOB_RATE_LIMIT_EXCEEDED": (
            "Wait for the rolling one-hour window to clear, then retry."
        ),
        "OUTPUT_EXISTS": "Choose a new --out directory or pass --overwrite.",
        "UPLOAD_TOO_LARGE": "Reduce the PDF size or raise the server upload limit.",
        "PAGE_LIMIT_EXCEEDED": "Split the input or raise the server page limit.",
        "QUEUE_UNAVAILABLE": "Restore Redis/worker availability, then retry with the same key.",
        "JOB_TIMEOUT": (
            "The job is still recoverable; inspect details.job_id with GET /v1/jobs/{id}."
        ),
    }
    if error.code in fixes:
        return fixes[error.code]
    if error.retryable:
        return "Retry after checking AgentP readiness and worker health."
    return "Inspect the structured details below and correct the request or input document."


def _run_packet(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _read_manifest(args.manifest)
    order = args.order or ("manifest" if manifest is not None else "as_provided")
    api_url = args.api_url or os.getenv("AGENTP_API_URL") or "http://localhost:8000"
    api_key = args.api_key or os.getenv("AGENTP_API_KEY") or os.getenv("AGENTP_KEY")
    if not api_key:
        raise AgentPClientError(
            "API_KEY_MISSING",
            "No AgentP API key was provided.",
            details={"environment_variable": "AGENTP_API_KEY"},
        )
    if args.timeout <= 0 or args.poll_interval < 0:
        raise AgentPClientError(
            "INVALID_TIMEOUT",
            "--timeout must be positive and --poll-interval cannot be negative.",
        )

    progress = None
    if not args.quiet and not args.json_output:
        def report_progress(message: str) -> None:
            print(message, file=sys.stderr)

        progress = report_progress

    with AgentPClient(api_url=api_url, api_key=api_key) as client:
        result = client.run_packet(
            args.documents,
            output_dir=args.out,
            order=order,
            labels=args.labels,
            manifest=manifest,
            allow_unlisted=args.allow_unlisted,
            language=args.language,
            deskew=not args.no_deskew,
            idempotency_key=args.idempotency_key,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            overwrite=args.overwrite,
            progress=progress,
        )
    return result.as_dict()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except AgentPClientError as exc:
        payload = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.retryable,
                "fix": _suggested_fix(exc),
            }
        }
        if getattr(args, "json_output", False):
            print(json.dumps(payload, indent=2), file=sys.stderr)
        else:
            print(f"AgentP error [{exc.code}]: {exc.message}", file=sys.stderr)
            print(f"Fix: {payload['error']['fix']}", file=sys.stderr)
            if exc.details:
                print(f"Details: {json.dumps(exc.details, sort_keys=True)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Any submitted job continues running in AgentP.", file=sys.stderr)
        return 130

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"Packet ready: {result['job_id']} ({result['status']})")
        for filename, path in result["outputs"].items():
            print(f"  {filename}: {path}")
        if result["warnings"]:
            print(f"  warnings: {len(result['warnings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
