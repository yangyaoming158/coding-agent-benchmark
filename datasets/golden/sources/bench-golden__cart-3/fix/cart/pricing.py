"""金额计算。

金额一律用**分**做整数运算，不用浮点数存钱。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def discounted_cents(amount_cents: int, percent: int) -> int:
    """按百分比打折后的金额（分），不足一分的部分四舍五入。

    >>> discounted_cents(1000, 10)
    900
    >>> discounted_cents(995, 10)
    896

    用 Decimal 而不是 `int(...)`：`int()` 是向零截断，995 打九折得 895.5，
    截断成 895，比应收少一分。差一分在对账时就是一条不平的流水。
    """
    if not 0 <= percent <= 100:
        raise ValueError(f"折扣百分比要在 0 到 100 之间：{percent}")
    exact = Decimal(amount_cents) * Decimal(100 - percent) / Decimal(100)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def line_total_cents(unit_price_cents: int, quantity: int) -> int:
    """一行商品的小计。"""
    if quantity < 0:
        raise ValueError(f"数量不能是负数：{quantity}")
    return unit_price_cents * quantity
