"""解析 CSV 的单独一行。

用在日志导入功能里：每读到一行就调 `parse_line()` 切成字段。
"""

from __future__ import annotations


def parse_line(line: str) -> list[str]:
    """把一行 CSV 切成字段列表。

    >>> parse_line("a,b,c")
    ['a', 'b', 'c']
    """
    return line.rstrip("\n").split(",")
