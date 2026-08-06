import pytest

from app.operations.base import KnownOperationError
from app.operations.split import parse_page_ranges


def test_parse_single_and_closed_ranges() -> None:
    assert parse_page_ranges(["1", "2-4"], 5) == [[0], [1, 2, 3]]


def test_parse_open_ended_ranges() -> None:
    assert parse_page_ranges(["-2", "4-"], 5) == [[0, 1], [3, 4]]


def test_parse_comma_separated_range() -> None:
    assert parse_page_ranges(["1,3-4"], 5) == [[0, 2, 3]]


def test_rejects_out_of_bounds_range() -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_page_ranges(["3-7"], 5)

    assert exc.value.code == "INVALID_PAGE_RANGE"
    assert exc.value.details["page_count"] == 5


@pytest.mark.parametrize("page_range", ["x", "1-x", "x-3", "1.5", "+2"])
def test_rejects_non_numeric_page_numbers(page_range: str) -> None:
    with pytest.raises(KnownOperationError) as exc:
        parse_page_ranges([page_range], 5)

    assert exc.value.code == "INVALID_PAGE_RANGE"
    assert exc.value.details["requested_range"] == page_range
