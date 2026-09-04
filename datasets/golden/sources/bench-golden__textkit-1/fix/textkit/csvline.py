"""解析 CSV 的单独一行。

用在日志导入功能里：每读到一行就调 `parse_line()` 切成字段。
"""

from __future__ import annotations


def parse_line(line: str) -> list[str]:
    """把一行 CSV 切成字段列表，双引号里的逗号不当分隔符。

    >>> parse_line("a,b,c")
    ['a', 'b', 'c']
    >>> parse_line('"a,b",c')
    ['a,b', 'c']

    引号内要表示一个双引号本身，写成两个连续的双引号（RFC 4180 的写法）。
    """
    text = line.rstrip("\n")
    fields: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_quotes:
            if char == '"':
                # 连续两个双引号表示一个字面量双引号，不是引号结束
                if index + 1 < len(text) and text[index + 1] == '"':
                    current.append('"')
                    index += 2
                    continue
                in_quotes = False
            else:
                current.append(char)
        elif char == '"':
            in_quotes = True
        elif char == ",":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    fields.append("".join(current))
    return fields
