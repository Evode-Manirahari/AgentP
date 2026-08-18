from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.operations.base import KnownOperationError, OperationOutput, OperationResult
from app.operations.merge import merge_pdfs
from app.operations.ocr import ocr_pdf
from app.operations.packet_manifest import PacketManifest, parse_packet_manifest
from app.operations.pdf_utils import count_pdf_pages, is_likely_scanned, sha256_path

PACKET_ORDERS = {"as_provided", "filename", "manifest"}
PACKET_FILENAME = "packet.pdf"
AUDIT_REPORT_FILENAME = "packet-audit-report.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _step(name: str, started_at: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"step": name, "started_at": started_at, "finished_at": _now(), "detail": detail}


def prepare_packet(
    input_paths: list[Path],
    workspace: Path,
    *,
    input_names: list[str] | None = None,
    language: str = "eng",
    deskew: bool = True,
    order: str = "as_provided",
    input_labels: list[str] | None = None,
    manifest: list[dict[str, Any]] | None = None,
    allow_unlisted: bool = False,
    timeout_seconds: int = 300,
) -> OperationResult:
    if len(input_paths) < 2:
        raise KnownOperationError(
            "NOT_ENOUGH_INPUTS",
            "Preparing a packet requires at least two PDF inputs.",
            details={"input_count": len(input_paths)},
        )
    if order not in PACKET_ORDERS:
        raise KnownOperationError(
            "INVALID_PACKET_ORDER",
            "Unsupported packet order.",
            details={"order": order, "allowed": sorted(PACKET_ORDERS)},
        )

    parsed_manifest: PacketManifest | None = None
    if order == "manifest":
        parsed_manifest = parse_packet_manifest(
            input_count=len(input_paths),
            input_labels=input_labels,
            manifest=manifest,
            allow_unlisted=allow_unlisted,
        )
    elif input_labels is not None or manifest is not None or allow_unlisted:
        raise KnownOperationError(
            "PACKET_MANIFEST_NOT_ENABLED",
            "Packet labels and manifest settings require order='manifest'.",
            details={"order": order},
        )

    names = list(input_names or [])
    if len(names) != len(input_paths):
        names = [path.name for path in input_paths]

    inspect_started = _now()
    entries: list[dict[str, Any]] = [
        {
            "position": position,
            "filename": name,
            "page_count": count_pdf_pages(path),
            "sha256": sha256_path(path),
            "scanned": is_likely_scanned(path),
            "ocr_applied": False,
            "path": path,
        }
        for position, (path, name) in enumerate(zip(input_paths, names, strict=True), start=1)
    ]
    if parsed_manifest is not None:
        for entry, label in zip(entries, parsed_manifest.input_labels, strict=True):
            entry["label"] = label
    steps = [
        _step(
            "inspect",
            inspect_started,
            {
                "input_count": len(entries),
                "scanned_inputs": [entry["position"] for entry in entries if entry["scanned"]],
            },
        )
    ]

    ocr_started = _now()
    warnings: list[dict[str, Any]] = []
    for entry in entries:
        if not entry["scanned"]:
            continue
        ocr_path = workspace / f"packet-input-{entry['position']}-ocr.pdf"
        ocr_pdf(
            entry["path"],
            ocr_path,
            language=language,
            deskew=deskew,
            timeout_seconds=timeout_seconds,
        )
        entry["path"] = ocr_path
        entry["ocr_applied"] = True
        # OCR that yields almost no text usually means an unreadable scan, which is worth
        # reporting without failing an otherwise valid packet.
        if is_likely_scanned(ocr_path):
            warnings.append(
                {
                    "code": "LOW_TEXT_AFTER_OCR",
                    "message": "OCR completed but the document still contains very little text.",
                    "position": entry["position"],
                    "filename": entry["filename"],
                }
            )
    steps.append(
        _step(
            "ocr",
            ocr_started,
            {
                "ocr_applied_to": [
                    entry["position"] for entry in entries if entry["ocr_applied"]
                ],
                "language": language,
                "deskew": deskew,
            },
        )
    )

    organize_started = _now()
    if order == "manifest":
        assert parsed_manifest is not None
        entries_by_position = {entry["position"]: entry for entry in entries}
        ordered = [
            entries_by_position[position] for position in parsed_manifest.ordered_positions()
        ]
    elif order == "filename":
        ordered = sorted(entries, key=lambda entry: (entry["filename"].lower(), entry["position"]))
    else:
        ordered = list(entries)
    sequence = [entry["position"] for entry in ordered]
    organize_detail: dict[str, Any] = {"order": order, "sequence": sequence}
    if parsed_manifest is not None:
        organize_detail.update(
            {
                "manifest": parsed_manifest.definition(),
                "manifest_validation": parsed_manifest.validation(),
                "unlisted_labels": list(parsed_manifest.unlisted_labels),
            }
        )
    steps.append(_step("organize", organize_started, organize_detail))

    merge_started = _now()
    packet_path = workspace / PACKET_FILENAME
    merge_result = merge_pdfs([entry["path"] for entry in ordered], packet_path)
    packet_pages = merge_result.outputs[0].page_count
    steps.append(
        _step("merge", merge_started, {"sequence": sequence, "page_count": packet_pages})
    )

    total_input_pages = sum(entry["page_count"] for entry in entries)
    ocr_applied_to = [entry["position"] for entry in entries if entry["ocr_applied"]]
    report_parameters: dict[str, Any] = {
        "language": language,
        "deskew": deskew,
        "order": order,
    }
    if parsed_manifest is not None:
        report_parameters.update(
            {
                "manifest": parsed_manifest.definition(),
                "allow_unlisted": parsed_manifest.allow_unlisted,
            }
        )

    report: dict[str, Any] = {
        "workflow": "prepare_packet",
        "parameters": report_parameters,
        "inputs": [
            {key: value for key, value in entry.items() if key != "path"} for entry in entries
        ],
        "sequence": sequence,
        "steps": steps,
        "output": {"filename": PACKET_FILENAME, "page_count": packet_pages},
        "warnings": warnings,
        "summary": {
            "input_count": len(entries),
            "total_input_pages": total_input_pages,
            "ocr_applied_count": len(ocr_applied_to),
            "warning_count": len(warnings),
        },
    }
    if parsed_manifest is not None:
        report["manifest_validation"] = parsed_manifest.validation()
        report["unlisted_labels"] = list(parsed_manifest.unlisted_labels)
    report_path = workspace / AUDIT_REPORT_FILENAME
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return OperationResult(
        outputs=[
            OperationOutput(
                path=packet_path,
                filename=PACKET_FILENAME,
                page_count=packet_pages,
                metadata={"sequence": sequence, "expected_pages": total_input_pages},
            ),
            OperationOutput(
                path=report_path,
                filename=AUDIT_REPORT_FILENAME,
                mime_type="application/json",
                metadata={"warning_count": len(warnings)},
            ),
        ],
        metadata={
            "workflow": "prepare_packet",
            "order": order,
            "sequence": sequence,
            "ocr_applied_to_inputs": ocr_applied_to,
            "total_input_pages": total_input_pages,
            "warnings": warnings,
            **(
                {
                    "manifest": parsed_manifest.definition(),
                    "manifest_validation": parsed_manifest.validation(),
                    "unlisted_labels": list(parsed_manifest.unlisted_labels),
                }
                if parsed_manifest is not None
                else {}
            ),
        },
    )
