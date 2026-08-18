from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.operations.base import KnownOperationError

PACKET_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
MAX_PACKET_SECTIONS = 100
SECTION_KEYS = {"label", "min_count", "max_count"}


@dataclass(frozen=True)
class PacketSection:
    label: str
    min_count: int
    max_count: int | None

    def definition(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "min_count": self.min_count,
            "max_count": self.max_count,
        }


@dataclass(frozen=True)
class PacketManifest:
    input_labels: tuple[str, ...]
    sections: tuple[PacketSection, ...]
    allow_unlisted: bool

    @property
    def counts(self) -> Counter[str]:
        return Counter(self.input_labels)

    @property
    def unlisted_labels(self) -> tuple[str, ...]:
        known = {section.label for section in self.sections}
        return tuple(dict.fromkeys(label for label in self.input_labels if label not in known))

    def ordered_positions(self) -> tuple[int, ...]:
        rank = {section.label: index for index, section in enumerate(self.sections)}
        fallback_rank = len(rank)
        return tuple(
            sorted(
                range(1, len(self.input_labels) + 1),
                key=lambda position: (
                    rank.get(self.input_labels[position - 1], fallback_rank),
                    position,
                ),
            )
        )

    def definition(self) -> list[dict[str, Any]]:
        return [section.definition() for section in self.sections]

    def validation(self) -> list[dict[str, Any]]:
        counts = self.counts
        return [
            {
                **section.definition(),
                "actual_count": counts[section.label],
                "satisfied": counts[section.label] >= section.min_count
                and (section.max_count is None or counts[section.label] <= section.max_count),
            }
            for section in self.sections
        ]


def _invalid_manifest(message: str, *, details: dict[str, Any]) -> KnownOperationError:
    return KnownOperationError("INVALID_PACKET_MANIFEST", message, details=details)


def _validate_label(value: object, *, location: str) -> str:
    if not isinstance(value, str) or PACKET_LABEL_PATTERN.fullmatch(value) is None:
        raise KnownOperationError(
            "INVALID_PACKET_LABEL",
            "Packet labels must start with a lowercase letter and contain only lowercase "
            "letters, numbers, '.', '_', or '-'.",
            details={"location": location, "value": value},
        )
    return value


def _validate_count(
    value: object,
    *,
    field: str,
    label: str,
    allow_none: bool,
) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_manifest(
            f"{field} must be a non-negative integer{', or null' if allow_none else ''}.",
            details={"label": label, "field": field, "value": value},
        )
    return value


def parse_packet_manifest(
    *,
    input_count: int,
    input_labels: object,
    manifest: object,
    allow_unlisted: object = False,
) -> PacketManifest:
    if not isinstance(allow_unlisted, bool):
        raise _invalid_manifest(
            "allow_unlisted must be a boolean.",
            details={"field": "allow_unlisted", "value": allow_unlisted},
        )

    if not isinstance(input_labels, list) or len(input_labels) != input_count:
        raise KnownOperationError(
            "INVALID_PACKET_LABELS",
            "Manifest ordering requires exactly one label for every input document.",
            details={
                "input_count": input_count,
                "label_count": len(input_labels) if isinstance(input_labels, list) else None,
            },
        )
    validated_labels = tuple(
        _validate_label(label, location=f"inputs[{index}].label")
        for index, label in enumerate(input_labels)
    )

    if not isinstance(manifest, list) or not manifest or len(manifest) > MAX_PACKET_SECTIONS:
        raise _invalid_manifest(
            "manifest must contain between 1 and 100 section definitions.",
            details={
                "section_count": len(manifest) if isinstance(manifest, list) else None,
                "max_sections": MAX_PACKET_SECTIONS,
            },
        )

    sections: list[PacketSection] = []
    seen: set[str] = set()
    for index, raw_section in enumerate(manifest):
        if not isinstance(raw_section, dict):
            raise _invalid_manifest(
                "Every manifest section must be an object.",
                details={"index": index, "value": raw_section},
            )
        unknown = set(raw_section) - SECTION_KEYS
        if unknown:
            raise _invalid_manifest(
                "A manifest section included unsupported fields.",
                details={"index": index, "unsupported": sorted(unknown)},
            )

        label = _validate_label(raw_section.get("label"), location=f"manifest[{index}].label")
        if label in seen:
            raise _invalid_manifest(
                "Every manifest label must be unique.",
                details={"label": label, "index": index},
            )
        seen.add(label)

        min_count = _validate_count(
            raw_section.get("min_count", 1),
            field="min_count",
            label=label,
            allow_none=False,
        )
        max_count = _validate_count(
            raw_section.get("max_count"),
            field="max_count",
            label=label,
            allow_none=True,
        )
        assert min_count is not None
        if max_count is not None and max_count < min_count:
            raise _invalid_manifest(
                "max_count cannot be less than min_count.",
                details={"label": label, "min_count": min_count, "max_count": max_count},
            )
        sections.append(PacketSection(label, min_count, max_count))

    parsed = PacketManifest(validated_labels, tuple(sections), allow_unlisted)
    if parsed.unlisted_labels and not allow_unlisted:
        raise KnownOperationError(
            "PACKET_LABEL_NOT_IN_MANIFEST",
            "One or more input labels are not declared in the packet manifest.",
            details={"labels": list(parsed.unlisted_labels)},
        )

    counts = parsed.counts
    violations = [
        {
            **section.definition(),
            "actual_count": counts[section.label],
            "reason": (
                "below_minimum"
                if counts[section.label] < section.min_count
                else "above_maximum"
            ),
        }
        for section in parsed.sections
        if counts[section.label] < section.min_count
        or (section.max_count is not None and counts[section.label] > section.max_count)
    ]
    if violations:
        raise KnownOperationError(
            "PACKET_MANIFEST_COUNT_MISMATCH",
            "The input documents do not satisfy the packet manifest counts.",
            details={"violations": violations},
        )
    return parsed
