"""金额计算。

金额一律用**分**做整数运算，不用浮点数存钱。
"""

from __future__ import annotations


def discounted_cents(amount_cents: int, percent: int) -> int:
    """按百分比打折后的金额（分）。

    >>> discounted_cents(1000, 10)
    900
    """
    return int(amount_cents * (100 - percent) / 100)


def line_total_cents(unit_price_cents: int, quantity: int) -> int:
    """一行商品的小计。"""
    if quantity < 0:
        raise ValueError(f"数量不能是负数：{quantity}")
    return unit_price_cents * quantity
