"""Token 单价与成本估算。

缓存命中的 token 已经包含在 ``tokens_input`` 里，不能再加一次。统一公式是：

    普通输入 = 输入 - 缓存读取
    成本 = (普通输入 × 输入单价 + 缓存读取 × 缓存单价 + 输出 × 输出单价) / 1_000_000

这里保持纯函数，不读数据库、不看具体 Agent。适配器自报成本时不经过本模块；
只有平台需要把 ``unavailable`` 补成 ``estimated`` 时才调用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

TOKENS_PER_MTOK = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class TokenPrices:
    """每百万 token 的美元单价；三项必须齐全才能估算。"""

    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    cache_read_per_mtok: Decimal | None = None

    @property
    def complete(self) -> bool:
        """三项价格是否齐全、有限且非负。"""
        values = (self.input_per_mtok, self.output_per_mtok, self.cache_read_per_mtok)
        return all(value is not None and value.is_finite() and value >= 0 for value in values)

    def as_manifest(self) -> dict[str, str | None]:
        """转成 JSON 可稳定保存的十进制字符串，避免 float 改变价目。"""
        return {
            "input_per_mtok": _decimal_text(self.input_per_mtok),
            "output_per_mtok": _decimal_text(self.output_per_mtok),
            "cache_read_per_mtok": _decimal_text(self.cache_read_per_mtok),
        }

    @classmethod
    def from_manifest(cls, value: object) -> TokenPrices | None:
        """从 manifest 的价格块恢复；块不存在表示老 manifest，返回 ``None``。"""
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(
                input_per_mtok=_optional_decimal(value.get("input_per_mtok")),
                output_per_mtok=_optional_decimal(value.get("output_per_mtok")),
                cache_read_per_mtok=_optional_decimal(value.get("cache_read_per_mtok")),
            )
        except (ValueError, TypeError, ArithmeticError):
            return None


def estimate_cost_usd(
    *,
    tokens_input: int | None,
    tokens_output: int | None,
    tokens_cache_read: int | None,
    prices: TokenPrices,
) -> Decimal | None:
    """按三档单价估算成本；数据不完整或自相矛盾时返回 ``None``。

    ``tokens_cache_read`` 是输入的一部分，所以必须满足
    ``0 <= tokens_cache_read <= tokens_input``。不满足时拒绝估算，不能用
    ``max(0, ...)`` 悄悄修数据，那会把适配器解析错误藏起来。
    """
    if not prices.complete:
        return None
    tokens = (tokens_input, tokens_output, tokens_cache_read)
    if any(value is None or value < 0 for value in tokens):
        return None
    assert tokens_input is not None
    assert tokens_output is not None
    assert tokens_cache_read is not None
    if tokens_cache_read > tokens_input:
        return None
    assert prices.input_per_mtok is not None
    assert prices.output_per_mtok is not None
    assert prices.cache_read_per_mtok is not None
    uncached_input = tokens_input - tokens_cache_read
    return (
        Decimal(uncached_input) * prices.input_per_mtok
        + Decimal(tokens_cache_read) * prices.cache_read_per_mtok
        + Decimal(tokens_output) * prices.output_per_mtok
    ) / TOKENS_PER_MTOK


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


__all__ = ["TokenPrices", "estimate_cost_usd"]
