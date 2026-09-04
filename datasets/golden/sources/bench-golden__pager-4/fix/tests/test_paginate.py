import pytest

from pager.paginate import page_count, page_slice

ITEMS = [1, 2, 3, 4, 5, 6, 7]


def test_first_page():
    assert page_slice(ITEMS, 1, 3) == [1, 2, 3]


def test_middle_page():
    assert page_slice(ITEMS, 2, 3) == [4, 5, 6]


def test_last_page_can_be_short():
    assert page_slice(ITEMS, 3, 3) == [7]


def test_page_past_the_end_is_empty():
    assert page_slice(ITEMS, 9, 3) == []


def test_page_count_rounds_up():
    assert page_count(7, 3) == 3


def test_page_count_rejects_zero_per_page():
    with pytest.raises(ValueError):
        page_count(7, 0)


def test_page_zero_is_rejected():
    with pytest.raises(ValueError):
        page_slice(ITEMS, 0, 3)


def test_negative_page_is_rejected():
    with pytest.raises(ValueError):
        page_slice(ITEMS, -1, 3)


def test_page_slice_rejects_zero_per_page():
    with pytest.raises(ValueError):
        page_slice(ITEMS, 1, 0)
