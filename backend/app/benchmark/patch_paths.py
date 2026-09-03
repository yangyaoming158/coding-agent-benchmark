"""从 unified diff 里解析出被改动的文件路径（协议 C-74）。

用途有三个：

1. 推导题目的 `test_patch_paths` —— 这份清单会被并进受保护路径（C-42 最后一条），
   因为有些题目的测试改动会带上名字完全不像测试的 fixture 文件，靠通配符匹配不到。
2. 校验 `test_patch` 只碰了测试文件、`gold_patch` 没碰受保护文件（C-64）。
3. 导入题目时**重算一遍**和已存清单比对，对不上就拒收（C-74 第 6 条防篡改）。

## 为什么不能按行首前缀直接 grep

diff 的**内容行**里可能出现和文件头一模一样的前缀。删掉一行 `-- foo` 之后，
diff 里那行长这样：

    --- foo

按 `line.startswith("--- ")` 抓路径的话，就会把 `foo` 当成一个被改的文件。
所以这里按 hunk 头 `@@ -a,b +c,d @@` 声明的行数**精确数过去**，
数完才回到"找文件头"的状态。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.protected_paths import normalize_path

#: hunk 头：`@@ -12,7 +12,9 @@ def foo():`。省略 `,n` 时表示 1 行。
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: `diff --git a/x b/y`，两边路径都可能被 git 用双引号包起来（含空格或非 ASCII 时）。
_DIFF_GIT = re.compile(
    r'^diff --git (?:"(?P<qa>(?:[^"\\]|\\.)*)"|(?P<a>\S+)) '
    r'(?:"(?P<qb>(?:[^"\\]|\\.)*)"|(?P<b>\S+))$'
)

#: 这些行后面直接跟一个路径，没有 a/ b/ 前缀。
_PLAIN_PATH_PREFIXES = ("rename from ", "rename to ", "copy from ", "copy to ")


def _unquote(path: str) -> str:
    r"""还原 git 的 C 风格转义。

    路径里有空格、引号或非 ASCII 字符时，git 会写成 `"a/\346\265\213.py"` 这种形式：
    非 ASCII 字节被转成三位八进制。不还原的话，中文文件名会变成一串反斜杠数字，
    和 `test_patch_paths` 里存的真实路径对不上，第 6 条的防篡改校验就会误报。
    """
    if not (path.startswith('"') and path.endswith('"') and len(path) >= 2):
        return path
    body = path[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        escape = body[index]
        if escape.isdigit():  # 三位八进制
            out.append(int(body[index : index + 3], 8))
            index += 3
        else:
            out.extend({"n": b"\n", "t": b"\t"}.get(escape, escape.encode("utf-8")))
            index += 1
    return out.decode("utf-8", errors="replace")


def _strip_ab_prefix(path: str) -> str:
    """去掉 `a/` `b/` 前缀。`/dev/null` 原样返回，由调用方跳过。"""
    if path == "/dev/null":
        return path
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


def _header_path(rest: str) -> str | None:
    """解析 `--- ` / `+++ ` 后面那截，返回路径；`/dev/null` 返回 None。

    非 git 生成的 diff 会在路径后面跟一个 tab 和时间戳，要切掉。
    """
    candidate = rest.split("\t", 1)[0].strip()
    if not candidate:
        return None
    candidate = _strip_ab_prefix(_unquote(candidate))
    return None if candidate == "/dev/null" else candidate


def iter_patch_paths(diff: str) -> Iterable[str]:
    """按出现顺序吐出补丁里所有被改动的路径（未归一化、未去重）。

    重命名和复制会同时吐出旧路径和新路径（C-74 第 4 条）——只记新路径的话，
    AI 把 `tests/test_a.py` 改名成 `helper.py` 再改内容就绕过去了（C-62）。
    """
    remaining_old = 0
    remaining_new = 0

    for line in diff.splitlines():
        # ── 正在数 hunk 里的内容行 ──
        if remaining_old > 0 or remaining_new > 0:
            if line.startswith("\\"):  # "\ No newline at end of file"，不占行数
                continue
            marker = line[:1]
            if marker == "-":
                remaining_old -= 1
            elif marker == "+":
                remaining_new -= 1
            elif marker in (" ", ""):  # 空字符串是尾随空格被编辑器吃掉的上下文行
                remaining_old -= 1
                remaining_new -= 1
            else:
                # 行数没数完就冒出别的东西，说明补丁被截断或被人改过。
                # 清零回到"找文件头"的状态，比按错误的计数继续硬数安全。
                remaining_old = remaining_new = 0
            remaining_old = max(remaining_old, 0)
            remaining_new = max(remaining_new, 0)
            continue

        # ── 文件头 ──
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            remaining_old = int(hunk.group(2) or 1)
            remaining_new = int(hunk.group(4) or 1)
            continue

        if line.startswith("--- ") or line.startswith("+++ "):
            path = _header_path(line[4:])
            if path:
                yield path
            continue

        for prefix in _PLAIN_PATH_PREFIXES:
            if line.startswith(prefix):
                yield _unquote(line[len(prefix) :].strip())
                break
        else:
            match = _DIFF_GIT.match(line)
            if match:
                for group in ("qa", "a", "qb", "b"):
                    raw = match.group(group)
                    if raw is None:
                        continue
                    path = _strip_ab_prefix(_unquote(f'"{raw}"' if group[0] == "q" else raw))
                    if path != "/dev/null":
                        yield path


def derive_patch_paths(diff: str) -> list[str]:
    """补丁改了哪些文件，归一化 + 排序 + 去重（C-74 第 2、3 条）。

    空补丁返回空列表，不报错——空补丁是合法输入（比如被测 AI 什么都没改）。
    """
    paths = {normalize_path(p) for p in iter_patch_paths(diff)}
    return sorted(p for p in paths if p)


__all__ = ["derive_patch_paths", "iter_patch_paths"]
