"""从 unified diff 里解析出被改动的文件路径（协议 C-74）。

**为什么在 `domain` 而不是 `benchmark`**：`app.runner` 在分层里压在 `app.benchmark`
下面（`pyproject.toml` 的 import-linter 契约），import 不到 benchmark 里的东西。
而补丁归一化（E3-T3）在 runner 里，它要按文件段拆 diff，用的是同一套解析规则。
放在 domain 是让两边共用同一个解析器 —— 各写一份的话，两个 diff 解析器一定会漂。

用途有四个：

1. 推导题目的 `test_patch_paths` —— 这份清单会被并进受保护路径（C-42 最后一条），
   因为有些题目的测试改动会带上名字完全不像测试的 fixture 文件，靠通配符匹配不到。
2. 校验 `test_patch` 只碰了测试文件、`gold_patch` 没碰受保护文件（C-64）。
3. 导入题目时**重算一遍**和已存清单比对，对不上就拒收（C-74 第 6 条防篡改）。
4. 补丁归一化（E3-T3）按文件段拆 diff，判断每一段该留还是该丢。

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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

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

#: 不带路径的文件操作标志。有它的段即使一个 hunk 都没有也是真改动。
_FILE_OP_PREFIXES = ("new file mode ", "deleted file mode ")

#: 只改权限、不改内容的标志。两行成对出现，中间没有任何 hunk。
_MODE_PREFIXES = ("old mode ", "new mode ")

#: 二进制补丁的标志行。`git diff` 默认打印第一种；加了 `--binary` 打印第二种，
#: 后面跟一大坨 base85。两种都认，因为归一化要把二进制段整段丢掉。
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


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


@dataclass(frozen=True, slots=True)
class DiffSection:
    """补丁里属于**一个文件**的那一段（从 `diff --git` 到下一个 `diff --git`）。

    补丁归一化（E3-T3）以段为单位做取舍：一段要么整个留下、要么整个丢掉。
    **不能按行取舍** —— 删掉 hunk 里的几行之后，hunk 头声明的行数就和实际行数
    对不上了，`git apply` 会报 `corrupt patch`。
    """

    #: 这一段的原文，行尾统一成 LF、末尾带换行。所有段拼起来就是一个合法补丁。
    text: str
    #: 段里出现过的所有路径，**未归一化、未去重、按出现顺序**。
    #: 改名和复制会有两个 —— 协议 C-62 要求旧路径和新路径都看。
    raw_paths: tuple[str, ...]
    lines_added: int
    lines_deleted: int
    hunk_count: int
    is_binary: bool
    #: 新建 / 删除 / 改名 / 复制。有这个标志的段即使没有 hunk 也是真改动。
    has_file_operation: bool
    has_mode_change: bool
    #: 这一段是**新建**文件。噪声过滤要用它区分"Agent 新掉下来的碎屑"和
    #: "仓库本来就跟踪着的文件"——后者即使名字像产物也是真内容，不能当噪声丢。
    is_new_file: bool

    @property
    def paths(self) -> tuple[str, ...]:
        """归一化、去重、排序之后的路径。判断受保护与否用这个。"""
        return tuple(sorted({normalize_path(p) for p in self.raw_paths if p}))

    @property
    def is_mode_change_only(self) -> bool:
        """只改了文件权限，内容一个字节没动 —— C-08b 里说的"空 mode 变更"。"""
        return self.has_mode_change and not self.has_content

    @property
    def has_content(self) -> bool:
        """这一段有没有东西可打。三者皆无就是一段空改动。"""
        return bool(self.hunk_count) or self.is_binary or self.has_file_operation


class _SectionBuilder:
    """边走边攒一个 `DiffSection`。

    单独一个类而不是一堆局部变量：hunk 行数的计数状态和路径、统计要一起转移，
    散成十个局部变量之后，"新起一段时哪些该清零"就成了容易漏的地方。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.raw_paths: list[str] = []
        self.lines_added = 0
        self.lines_deleted = 0
        self.hunk_count = 0
        self.is_binary = False
        self.has_file_operation = False
        self.has_mode_change = False
        self.is_new_file = False
        #: 这一段已经见过 `--- ` 了。非 git 生成的 diff 没有 `diff --git` 行，
        #: 只能靠"又来一个 `---`"判断换了文件。
        self.seen_old_header = False
        self.remaining_old = 0
        self.remaining_new = 0

    @property
    def counting(self) -> bool:
        """还在 hunk 里数内容行。"""
        return self.remaining_old > 0 or self.remaining_new > 0

    def consume_content_line(self, line: str) -> None:
        """hunk 内部的一行。**原样保留**，包括行尾可能的 `\r`。

        为什么内容行不做行尾归一化：`+foo\r` 有两种可能，一是补丁文件本身是
        CRLF 存的，二是这一行真的要往文件里写一个 CR。从补丁里分不出来，
        而猜错第二种就是悄悄改掉了 AI 的修改内容。结构行没有这个歧义，
        那边照常归一化（见 `consume_header_line`）。
        """
        self.lines.append(line)
        if line.startswith("\\"):  # "\ No newline at end of file"，不占行数
            return
        marker = line[:1]
        if marker == "-":
            self.remaining_old -= 1
            self.lines_deleted += 1
        elif marker == "+":
            self.remaining_new -= 1
            self.lines_added += 1
        elif marker in (" ", "", "\r"):  # 空串是尾随空格被编辑器吃掉的上下文行
            self.remaining_old -= 1
            self.remaining_new -= 1
        else:
            # 行数没数完就冒出别的东西，说明补丁被截断或被人改过。
            # 清零回到"找文件头"的状态，比按错误的计数继续硬数安全。
            self.remaining_old = self.remaining_new = 0
        self.remaining_old = max(self.remaining_old, 0)
        self.remaining_new = max(self.remaining_new, 0)

    def consume_header_line(self, line: str) -> None:
        """hunk 之外的一行：文件头、hunk 头、各种标志行。

        这里做行尾归一化（去掉尾部 `\r`）—— 结构行的内容是 git 自己写的，
        尾部 CR 只可能来自"这个补丁文件是 CRLF 存的"，不可能是有意义的数据。
        """
        line = line[:-1] if line.endswith("\r") else line
        self.lines.append(line)

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            self.hunk_count += 1
            self.remaining_old = int(hunk.group(2) or 1)
            self.remaining_new = int(hunk.group(4) or 1)
            return

        if line.startswith(("--- ", "+++ ")):
            self.seen_old_header = self.seen_old_header or line.startswith("--- ")
            path = _header_path(line[4:])
            if path:
                self.raw_paths.append(path)
            return

        for prefix in _PLAIN_PATH_PREFIXES:
            if line.startswith(prefix):
                self.raw_paths.append(_unquote(line[len(prefix) :].strip()))
                self.has_file_operation = True
                return

        if line.startswith(_FILE_OP_PREFIXES):
            self.has_file_operation = True
            self.is_new_file = self.is_new_file or line.startswith("new file mode ")
            return
        if line.startswith(_MODE_PREFIXES):
            self.has_mode_change = True
            return
        if line.startswith(_BINARY_MARKERS):
            self.is_binary = True
            return

        match = _DIFF_GIT.match(line)
        if match:
            for group in ("qa", "a", "qb", "b"):
                raw = match.group(group)
                if raw is None:
                    continue
                path = _strip_ab_prefix(_unquote(f'"{raw}"' if group[0] == "q" else raw))
                if path != "/dev/null":
                    self.raw_paths.append(path)

    def build(self) -> DiffSection:
        return DiffSection(
            text="\n".join(self.lines) + "\n",
            raw_paths=tuple(self.raw_paths),
            lines_added=self.lines_added,
            lines_deleted=self.lines_deleted,
            hunk_count=self.hunk_count,
            is_binary=self.is_binary,
            has_file_operation=self.has_file_operation,
            has_mode_change=self.has_mode_change,
            is_new_file=self.is_new_file,
        )


def _starts_new_section(line: str, current: _SectionBuilder | None) -> bool:
    """这一行是不是一个新文件段的开头。只在 hunk 之外调用。"""
    if _DIFF_GIT.match(line):
        return True
    if line.startswith("--- "):
        # 非 git 生成的 diff（比如 `diff -u` 的输出）没有 `diff --git` 行。
        # 当前段已经见过一个 `---` 了，说明这是下一个文件的开头。
        return current is None or current.seen_old_header
    return False


def iter_diff_sections(diff: str) -> Iterator[DiffSection]:
    """把补丁按文件拆成一段一段。

    第一个文件段之前的东西一律丢掉 —— `git format-patch` 会在前面写邮件头和
    提交信息，那些不属于任何文件的改动，留着只会让归一化后的补丁哈希不稳定。

    按 `\n` 切而不是 `splitlines()`：后者还会在换页符、`\u2028` 这些字符上断行，
    而补丁内容里真的可能有这些字节。这个函数要把原文重新拼回去，断错一次就是
    一个打不上的补丁。
    """
    current: _SectionBuilder | None = None
    lines = diff.split("\n")
    # `split` 在末尾换行处会多出一个空串，它不是一行
    if lines and lines[-1] == "":
        lines.pop()

    for line in lines:
        if current is not None and current.counting:
            current.consume_content_line(line)
            continue
        probe = line[:-1] if line.endswith("\r") else line
        if _starts_new_section(probe, current):
            if current is not None and current.lines:
                yield current.build()
            current = _SectionBuilder()
        if current is None:
            continue  # 还没进入任何文件段，丢掉
        current.consume_header_line(line)

    if current is not None and current.lines:
        yield current.build()


def iter_patch_paths(diff: str) -> Iterable[str]:
    """按出现顺序吐出补丁里所有被改动的路径（未归一化、未去重）。

    重命名和复制会同时吐出旧路径和新路径（C-74 第 4 条）——只记新路径的话，
    AI 把 `tests/test_a.py` 改名成 `helper.py` 再改内容就绕过去了（C-62）。
    """
    for section in iter_diff_sections(diff):
        yield from section.raw_paths


def derive_patch_paths(diff: str) -> list[str]:
    """补丁改了哪些文件，归一化 + 排序 + 去重（C-74 第 2、3 条）。

    空补丁返回空列表，不报错——空补丁是合法输入（比如被测 AI 什么都没改）。
    """
    paths = {normalize_path(p) for p in iter_patch_paths(diff)}
    return sorted(p for p in paths if p)


__all__ = ["DiffSection", "derive_patch_paths", "iter_diff_sections", "iter_patch_paths"]
