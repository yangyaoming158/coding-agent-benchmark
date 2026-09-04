"""列表分页。

页码从 1 开始数，和接口文档、前端组件保持一致。
"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def page_slice(items: list[T], page: int, per_page: int) -> list[T]:
    """取第 `page` 页的内容。

    >>> page_slice([1, 2, 3, 4, 5], 2, 2)
    [3, 4]

    页码从 1 开始。`page` 小于 1 会算出负的起始下标，Python 的负数切片会从列表
    尾部取值 —— 那样第 0 页会莫名其妙地返回最后一页的内容，所以这里直接拒绝。
    """
    if page < 1:
        raise ValueError(f"页码从 1 开始：{page}")
    if per_page <= 0:
        raise ValueError(f"每页条数必须是正数：{per_page}")
    start = (page - 1) * per_page
    return items[start : start + per_page]


def page_count(total: int, per_page: int) -> int:
    """一共有多少页。"""
    if per_page <= 0:
        raise ValueError(f"每页条数必须是正数：{per_page}")
    return (total + per_page - 1) // per_page
