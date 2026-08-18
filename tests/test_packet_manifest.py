import pytest

from app.operations.base import KnownOperationError
from app.operations.packet_manifest import parse_packet_manifest


def test_manifest_orders_sections_and_preserves_order_within_a_section() -> None:
    parsed = parse_packet_manifest(
        input_count=4,
        input_labels=["statement", "application", "statement", "identity"],
        manifest=[
            {"label": "application", "min_count": 1, "max_count": 1},
            {"label": "identity", "min_count": 1, "max_count": 2},
            {"label": "statement", "min_count": 1},
        ],
    )

    assert parsed.ordered_positions() == (2, 4, 1, 3)
    assert parsed.validation() == [
        {
            "label": "application",
            "min_count": 1,
            "max_count": 1,
            "actual_count": 1,
            "satisfied": True,
        },
        {
            "label": "identity",
            "min_count": 1,
            "max_count": 2,
            "actual_count": 1,
            "satisfied": True,
        },
        {
            "label": "statement",
            "min_count": 1,
            "max_count": None,
            "actual_count": 2,
            "satisfied": True,
        },
    ]


def test_manifest_rejects_a_missing_required_section() -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_packet_manifest(
            input_count=2,
            input_labels=["application", "identity"],
            manifest=[
                {"label": "application"},
                {"label": "identity"},
                {"label": "statement", "min_count": 1},
            ],
        )

    assert exc.value.code == "PACKET_MANIFEST_COUNT_MISMATCH"
    assert exc.value.details["violations"] == [
        {
            "label": "statement",
            "min_count": 1,
            "max_count": None,
            "actual_count": 0,
            "reason": "below_minimum",
        }
    ]


def test_manifest_rejects_too_many_documents_in_a_section() -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_packet_manifest(
            input_count=3,
            input_labels=["application", "identity", "identity"],
            manifest=[
                {"label": "application"},
                {"label": "identity", "max_count": 1},
            ],
        )

    assert exc.value.code == "PACKET_MANIFEST_COUNT_MISMATCH"
    assert exc.value.details["violations"][0]["reason"] == "above_maximum"


def test_manifest_can_append_unlisted_labels_in_original_order() -> None:
    parsed = parse_packet_manifest(
        input_count=4,
        input_labels=["note", "identity", "application", "appendix"],
        manifest=[{"label": "application"}, {"label": "identity"}],
        allow_unlisted=True,
    )

    assert parsed.ordered_positions() == (3, 2, 1, 4)
    assert parsed.unlisted_labels == ("note", "appendix")


def test_manifest_rejects_unlisted_labels_by_default() -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_packet_manifest(
            input_count=2,
            input_labels=["application", "note"],
            manifest=[{"label": "application"}],
        )

    assert exc.value.code == "PACKET_LABEL_NOT_IN_MANIFEST"
    assert exc.value.details == {"labels": ["note"]}


@pytest.mark.parametrize(
    ("manifest", "expected_code"),
    [
        ([], "INVALID_PACKET_MANIFEST"),
        ([{"label": "identity"}, {"label": "identity"}], "INVALID_PACKET_MANIFEST"),
        ([{"label": "Identity"}], "INVALID_PACKET_LABEL"),
        ([{"label": "identity", "min_count": True}], "INVALID_PACKET_MANIFEST"),
        (
            [{"label": "identity", "min_count": 2, "max_count": 1}],
            "INVALID_PACKET_MANIFEST",
        ),
        ([{"label": "identity", "unexpected": 1}], "INVALID_PACKET_MANIFEST"),
    ],
)
def test_manifest_rejects_invalid_section_definitions(
    manifest: list[dict[str, object]],
    expected_code: str,
) -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_packet_manifest(
            input_count=1,
            input_labels=["identity"],
            manifest=manifest,
        )

    assert exc.value.code == expected_code


def test_manifest_requires_exactly_one_valid_label_per_input() -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_packet_manifest(
            input_count=2,
            input_labels=["identity"],
            manifest=[{"label": "identity"}],
        )

    assert exc.value.code == "INVALID_PACKET_LABELS"

