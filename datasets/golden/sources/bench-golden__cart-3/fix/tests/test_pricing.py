import pytest

from cart.pricing import discounted_cents, line_total_cents


def test_no_discount_keeps_amount():
    assert discounted_cents(1000, 0) == 1000


def test_full_discount_is_free():
    assert discounted_cents(1000, 100) == 0


def test_clean_ten_percent():
    assert discounted_cents(1000, 10) == 900


def test_line_total():
    assert line_total_cents(299, 3) == 897


def test_negative_quantity_rejected():
    with pytest.raises(ValueError):
        line_total_cents(299, -1)


def test_half_cent_rounds_up():
    assert discounted_cents(995, 10) == 896


def test_repeated_half_cents_do_not_drift():
    assert sum(discounted_cents(995, 10) for _ in range(100)) == 89600


def test_percent_above_hundred_rejected():
    with pytest.raises(ValueError):
        discounted_cents(1000, 120)


def test_negative_percent_rejected():
    with pytest.raises(ValueError):
        discounted_cents(1000, -5)
