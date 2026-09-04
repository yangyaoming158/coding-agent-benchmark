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
    """
    start = (page - 1) * per_page
    return items[start : start + per_page]


def page_count(total: int, per_page: int) -> int:
    """一共有多少页。"""
    if per_page <= 0:
        raise ValueError(f"每页条数必须是正数：{per_page}")
    return (total + per_page - 1) // per_page
