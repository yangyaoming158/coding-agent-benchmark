"""pytest 用例 ID 归一化（E4-T1，`06-judge-attribution.md` §11.3）。

**这是全项目公认最容易出静默 bug 的地方**（`AGENTS.md` §5.5、协议 C-13a）。

一句话说清楚风险：题目里写的是 `tests/test_a.py::test_x`，报告里出现的是
`./tests/test_a.py::test_x`。两个字符串不相等，匹配不上，这条用例就被判成
`MISSING`，进而整道题判成"没修好"。**程序不报任何错**，只有解决率莫名其妙地偏低。

所以这个模块只干一件事：把各种写法收敛成同一个规范形式

    仓库相对 POSIX 路径 :: [类名 ::]* 用例名[参数]

路径那一半复用 `app.domain.protected_paths.normalize_path()`，和受保护路径判断
用的是同一个函数。各写一份的话，两边迟早会漂 —— 而漂了之后，一边说"这个文件受保护"，
另一边说"这条用例不存在"，两个症状看起来毫无关系。

## junitxml 的 classname 是点分模块名，不是文件路径

这是本模块最核心的麻烦。2026-09-05 在开发机上用 pytest 9.1.1 实测：

    tests/sub/test_nested.py::test_deep
        → classname="tests.sub.test_nested"  name="test_deep"
    tests/test_shapes.py::TestGroup::test_method
        → classname="tests.test_shapes.TestGroup"  name="test_method"

于是 `a.b.C` 有歧义：可能是 `a/b/C.py`，也可能是 `a/b.py` 里的类 `C`。
**光看这一个字符串分不出来。**

两条出路，按顺序用：

1. **`file` 属性**。`-o junit_family=xunit1` 会额外写
   `file="tests/sub/test_nested.py"`，歧义当场消失。有它就用它。
   默认的 `xunit2` 没有这个属性 —— 所以不能只支持一种 family。
2. **按命名约定打分**（`_split_score`）。pytest 默认只收集 `test_*.py` / `*_test.py`
   里名字以 `Test` 开头的类，这两条约定足够挑出最可能的切分。
   剩下的切法**全部作为备选 ID 一起返回**，匹配时挨个试 —— 猜错一次的代价是
   一道题白判，多留几个备选的代价只是几十字节。

## 非 ASCII 参数会被转义

`test_param["带空格 的"]` 在 junitxml 里是
`name="test_param[\\u5e26\\u7a7a\\u683c \\u7684]"` —— 字面的反斜杠 u 序列，
不是真的中文。题目里的 F2P ID 如果写的是中文原文，就对不上。
`decode_junit_escapes()` 负责还原。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.domain.protected_paths import normalize_path

#: 用例 ID 里路径和节点的分隔符。pytest 从 2.x 起就是这个，不会变。
ID_SEPARATOR = "::"

#: junitxml 把非 ASCII 转义成的样子：字面的反斜杠 + `u` + 四位十六进制。
_ESCAPED_UNICODE = re.compile(r"\\u([0-9a-fA-F]{4})")

#: pytest 默认的 `python_files`：测试模块的文件名长这样。
_MODULE_PREFIX = "test_"
_MODULE_SUFFIX = "_test"
#: pytest 默认的 `python_classes`：只收集名字以 `Test` 开头的类。
_CLASS_PREFIX = "Test"


def decode_junit_escapes(text: str) -> str:
    r"""把 junitxml 里字面的 `\uXXXX` 还原成真正的字符。

    `test_param[\u5e26\u7a7a\u683c \u7684]` → `test_param[带空格 的]`。

    超出基本平面的字符（emoji 之类）会被拆成一对代理项，两个 `\uXXXX` 挨着写。
    先逐个转成字符，再用 utf-16 把代理对拼回去。落单的代理项拼不回来，
    那时原样返回 —— 造一个乱码字符会让这条 ID 永远匹配不上，比不动它更糟。

    只认恰好四位十六进制的形式，所以 Windows 路径 `C:\users` 不会被误伤
    （`sers` 不是十六进制）。
    """
    if "\\u" not in text:
        return text
    decoded = _ESCAPED_UNICODE.sub(lambda m: chr(int(m.group(1), 16)), text)
    if any("\ud800" <= ch <= "\udfff" for ch in decoded):
        try:
            return decoded.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeDecodeError:
            return text
    return decoded


def _relativize(path: str, repo_root: Path | str | None) -> str:
    """把仓库根底下的绝对路径切成相对路径。

    **纯字符串比较，不碰文件系统**：报告是在容器里生成的，里面的路径在本机根本不存在，
    用 `Path.relative_to` 之前那些 `resolve()` 会把符号链接跟丢。
    """
    if repo_root is None:
        return path
    root = str(repo_root).replace("\\", "/").rstrip("/")
    candidate = path.replace("\\", "/")
    if root and candidate.startswith(root + "/"):
        return candidate[len(root) + 1 :]
    return path


def normalize_test_id(raw: str, *, repo_root: Path | str | None = None) -> str:
    """把一条用例 ID 收敛成规范形式。

    覆盖的写法（每一种都有对应单测，见 `tests/unit/test_test_id_normalization.py`）：

    | 写法 | 例子 | 归一化结果 |
    |:---|:---|:---|
    | 相对路径 | `tests/test_a.py::test_x` | 原样 |
    | `./` 前缀 | `./tests/test_a.py::test_x` | `tests/test_a.py::test_x` |
    | 绝对路径 | `/repo/tests/test_a.py::test_x`（`repo_root=/repo`） | `tests/test_a.py::test_x` |
    | 反斜杠 | `tests\\test_a.py::test_x` | `tests/test_a.py::test_x` |
    | 多层目录 | `tests/a/b/test_c.py::test_x` | 原样 |
    | 类方法 | `tests/test_a.py::TestG::test_m` | 原样 |
    | 参数化 | `tests/test_a.py::test_x[1-2]` | 原样 |
    | 转义参数 | `tests/test_a.py::test_x[\\u5e26]` | `tests/test_a.py::test_x[带]` |

    **只有第一段当路径处理**，`::` 后面的部分逐字保留。参数值里可能有 `::`
    （`test_x[a::b]`），按原样切开再原样拼回去，结果不变。

    没给 `repo_root` 时绝对路径会被去掉开头的 `/`（`normalize_path` 的行为），
    变成 `repo/tests/test_a.py::test_x` 这种。它匹配不上题目里的相对 ID，
    但 `ParsedReport.resolve()` 还有一层按路径后缀兜底。能给 `repo_root` 就给。
    """
    text = decode_junit_escapes(raw.strip())
    if not text:
        return ""
    path, *nodes = text.split(ID_SEPARATOR)
    path = normalize_path(_relativize(path, repo_root))
    return ID_SEPARATOR.join([path, *nodes])


def id_path(test_id: str) -> str:
    """取用例 ID 的路径部分（`::` 之前）。"""
    return test_id.split(ID_SEPARATOR, 1)[0]


def id_node(test_id: str) -> str:
    """取用例 ID 的节点部分（`::` 之后），没有则返回空串。

    判断两条 ID 是不是"同一条用例、只是路径前缀不同"时用它。
    """
    parts = test_id.split(ID_SEPARATOR, 1)
    return parts[1] if len(parts) == 2 else ""


def module_path_from_dotted(dotted: str) -> str:
    """点分模块名还原成文件路径：`brk.test_broken` → `brk/test_broken.py`。

    收集失败的 testcase 用得上 —— 那种条目的 `classname` 是空的，
    `name` 里放的是点分模块名（实测，见 `tests/fixtures/reports/_record.py`）。

    传进来的已经是路径时原样归一化，不重复加后缀。
    """
    text = dotted.strip()
    if not text:
        return ""
    if "/" in text or "\\" in text or text.endswith(".py"):
        return normalize_path(text)
    return normalize_path(text.replace(".", "/")) + ".py"


@dataclass(frozen=True, slots=True)
class CaseId:
    """一条 junitxml testcase 还原出来的用例 ID。

    `alternates` 是 classname 的**其他切法**。有 `file` 属性时它是空的
    （那时没有歧义）；没有时按可信度排序，匹配不上主 ID 就挨个试。
    """

    #: 最可信的那一个，写进数据库的 `test_results.test_id` 用它。
    primary: str
    #: 其余切法，按可信度从高到低，不含 `primary`。
    alternates: tuple[str, ...]
    #: classname 有多种切法、又没有 `file` 属性可以裁决。
    #: 报告里只要有一条是 True，就值得建议给 test_command 加 `-o junit_family=xunit1`。
    ambiguous: bool

    @property
    def all_ids(self) -> tuple[str, ...]:
        return (self.primary, *self.alternates)


def _split_score(module_parts: list[str], class_parts: list[str]) -> int:
    """给一种切分打分，分越高越可信。

    两条依据都是 pytest 的默认收集规则：模块文件名是 `test_*.py` 或 `*_test.py`，
    类名以 `Test` 开头。项目自己改过 `python_classes` 的话第二条会失准，
    所以打分只用来排序，落选的切法仍然作为备选 ID 留着。
    """
    basename = module_parts[-1]
    score = 3 if basename.startswith(_MODULE_PREFIX) or basename.endswith(_MODULE_SUFFIX) else 0
    for part in class_parts:
        if part.startswith(_CLASS_PREFIX):
            score += 2
        elif part[:1].isupper():
            score += 1
        else:
            # 小写开头的段几乎不可能是类名，更像是被切错了的目录或模块名
            score -= 3
    return score


def _classname_splits(classname: str) -> list[tuple[list[str], list[str]]]:
    """列出 `a.b.C` 的所有"前 i 段是模块路径、其余是类名链"切法，按可信度排序。

    先按"模块段最多"生成（`a/b/C.py` → `a/b.py::C` → `a.py::b::C`），再按分数稳定排序。
    稳定排序保证同分时保持这个顺序，也就是**同分时优先当成模块**——
    对 `pkg.sub.mod` 这种全小写的名字，这是对的。
    """
    parts = classname.split(".")
    candidates = [(parts[:i], parts[i:]) for i in range(len(parts), 0, -1)]
    return sorted(candidates, key=lambda pair: -_split_score(*pair))


def _join_id(module_parts: list[str], class_parts: list[str], name: str) -> str:
    return ID_SEPARATOR.join(["/".join(module_parts) + ".py", *class_parts, name])


def junit_case_id(
    classname: str,
    name: str,
    file: str | None = None,
    *,
    repo_root: Path | str | None = None,
) -> CaseId:
    """把 junitxml 的 `classname` / `name` / `file` 还原成用例 ID。

    有 `file`（xunit1 family）时路径直接照抄，只需要把 classname 里多出来的
    尾巴当类名链。没有 `file`（默认的 xunit2）时按 `_classname_splits` 猜，
    并把其余切法当备选返回。

    `file` 和 `classname` 对不上时（rootdir 不等于仓库根就会这样），
    **路径以 `file` 为准**——它是 pytest 直接写下来的事实，classname 是推导出来的。
    """
    if not classname:
        # 收集失败的条目走的是这条路：classname 是空的，name 是点分模块名。
        # 调用方通常在此之前就把它们单独摘出去了，这里只保证不炸。
        return CaseId(normalize_test_id(name, repo_root=repo_root), (), False)

    if file:
        path = normalize_test_id(file, repo_root=repo_root)
        dotted = path.removesuffix(".py").replace("/", ".")
        agrees = True
        if classname == dotted:
            class_parts: list[str] = []
        elif classname.startswith(dotted + "."):
            class_parts = classname[len(dotted) + 1 :].split(".")
        else:
            # file 和 classname 对不上：rootdir 不等于仓库根时会这样。
            # 路径以 file 为准，但类名链是猜的，所以把其余切法留作备选。
            class_parts = _classname_splits(classname)[0][1]
            agrees = False
        primary = normalize_test_id(
            ID_SEPARATOR.join([path, *class_parts, name]), repo_root=repo_root
        )
        if agrees:
            return CaseId(primary, (), False)
        guessed = _guessed_ids(classname, name, repo_root=repo_root)
        return CaseId(primary, tuple(i for i in guessed if i != primary), True)

    ids = _guessed_ids(classname, name, repo_root=repo_root)
    return CaseId(ids[0], tuple(ids[1:]), len(ids) > 1)


def _guessed_ids(classname: str, name: str, *, repo_root: Path | str | None) -> list[str]:
    """纯靠 classname 猜出来的 ID，按可信度排序、去重。"""
    seen: dict[str, None] = {}
    for module_parts, class_parts in _classname_splits(classname):
        seen.setdefault(
            normalize_test_id(_join_id(module_parts, class_parts, name), repo_root=repo_root)
        )
    return list(seen)


__all__ = [
    "ID_SEPARATOR",
    "CaseId",
    "decode_junit_escapes",
    "id_node",
    "id_path",
    "junit_case_id",
    "module_path_from_dotted",
    "normalize_test_id",
]
