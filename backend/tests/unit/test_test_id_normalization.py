"""用例 ID 归一化的单测（E4-T1，AC 第 2 条）。

`AGENTS.md` §5.5 要求"必须有专门的单元测试覆盖至少 6 种 ID 写法"。这里覆盖 9 种：
相对路径、`./` 前缀、绝对路径、反斜杠、多层目录、类方法、参数化、
转义的非 ASCII 参数、重复斜杠。

为什么值得写这么细：归一化写错**不会报错**，只会让每道题都莫名其妙地失败
（协议 C-13a）。这类 bug 唯一的防线就是单测。
"""

from __future__ import annotations

import pytest

from app.judge.test_ids import (
    decode_junit_escapes,
    id_node,
    id_path,
    junit_case_id,
    module_path_from_dotted,
    normalize_test_id,
)

#: 规范形式。下面所有写法都必须归一化成它。
CANONICAL = "tests/test_a.py::test_x"


@pytest.mark.parametrize(
    ("shape", "raw"),
    [
        ("相对路径", "tests/test_a.py::test_x"),
        ("./ 前缀", "./tests/test_a.py::test_x"),
        ("重复斜杠", "tests//test_a.py::test_x"),
        ("中间的 . 段", "tests/./test_a.py::test_x"),
        ("反斜杠（Windows 风格）", r"tests\test_a.py::test_x"),
        ("前后空白", "  tests/test_a.py::test_x\n"),
        ("多余的前导斜杠", "/tests/test_a.py::test_x"),
    ],
)
def test_normalize_collapses_path_shapes(shape: str, raw: str) -> None:
    """七种路径写法都要收敛到同一个字符串。"""
    assert normalize_test_id(raw) == CANONICAL, shape


def test_normalize_absolute_path_with_repo_root() -> None:
    """绝对路径 + 仓库根 → 相对路径。容器里跑出来的报告就是这个形状。"""
    raw = "/workspace/tests/test_a.py::test_x"
    assert normalize_test_id(raw, repo_root="/workspace") == CANONICAL
    # 结尾多一个斜杠不影响
    assert normalize_test_id(raw, repo_root="/workspace/") == CANONICAL


def test_normalize_absolute_path_without_repo_root_keeps_prefix() -> None:
    """不给仓库根时，绝对路径只去掉开头的斜杠，前缀留着。

    这是有意的：瞎猜哪一段是仓库根会把 `home/u/repo` 也切掉，从此两个不同仓库的
    同名用例互相顶替。前缀留着，交给 `ParsedReport.resolve()` 那层按后缀匹配去兜，
    那层要求全局唯一，猜不准时宁可返回 None。
    """
    assert normalize_test_id("/home/u/repo/tests/test_a.py::test_x") == (
        "home/u/repo/tests/test_a.py::test_x"
    )


@pytest.mark.parametrize(
    ("shape", "raw", "expected"),
    [
        ("多层目录", "./tests/a/b/test_c.py::test_x", "tests/a/b/test_c.py::test_x"),
        ("类方法", "./tests/test_a.py::TestG::test_m", "tests/test_a.py::TestG::test_m"),
        ("参数化", "./tests/test_a.py::test_x[1-2]", "tests/test_a.py::test_x[1-2]"),
        (
            "参数化 + 类方法",
            "./tests/test_a.py::TestG::test_m[a-b]",
            "tests/test_a.py::TestG::test_m[a-b]",
        ),
        (
            "参数值里带空格",
            "./tests/test_a.py::test_x[带空格 的]",
            "tests/test_a.py::test_x[带空格 的]",
        ),
        (
            "参数值里带 ::",
            "./tests/test_a.py::test_x[a::b]",
            "tests/test_a.py::test_x[a::b]",
        ),
    ],
)
def test_normalize_keeps_node_part_verbatim(shape: str, raw: str, expected: str) -> None:
    """`::` 之后的部分逐字保留，只有路径那一段被动过。"""
    assert normalize_test_id(raw) == expected, shape


def test_normalize_decodes_escaped_non_ascii_parameter() -> None:
    r"""junitxml 把非 ASCII 参数写成字面的 `\uXXXX`，要还原成真字符。

    题目里的 F2P ID 写的是中文原文，不还原就永远匹配不上 —— 而且不报错，
    只会多出一条假的 MISSING。
    """
    escaped = r"tests/test_a.py::test_param[\u5e26\u7a7a\u683c \u7684]"
    assert normalize_test_id(escaped) == "tests/test_a.py::test_param[带空格 的]"


def test_decode_junit_escapes_handles_surrogate_pairs() -> None:
    """超出基本平面的字符被拆成一对代理项，要拼回去。"""
    assert decode_junit_escapes(r"test_x[\ud83d\ude00]") == "test_x[😀]"


def test_decode_junit_escapes_leaves_lone_surrogate_alone() -> None:
    """落单的代理项拼不回来，原样返回比造个乱码好。"""
    lone = r"test_x[\ud83d]"
    assert decode_junit_escapes(lone) == lone


def test_decode_junit_escapes_ignores_non_hex_backslash_u() -> None:
    r"""`\users` 不是转义序列（`sers` 不是十六进制），不能被误伤。"""
    assert decode_junit_escapes(r"test_x[C:\users]") == r"test_x[C:\users]"


def test_normalize_is_idempotent() -> None:
    """归一化两次和一次结果相同。

    这条比看起来重要：题目里的 ID 和报告里的 ID 会各自被归一化，有时还会被归一化
    两遍（比如从数据库读出来再对一次）。不幂等的话，比对的两边就可能停在不同形状上。
    """
    for raw in (
        "./tests/a//b/test_c.py::TestG::test_m[x y]",
        r"tests\test_a.py::test_param[\u5e26]",
        "/workspace/tests/test_a.py::test_x",
    ):
        once = normalize_test_id(raw)
        assert normalize_test_id(once) == once, raw


def test_normalize_empty_string() -> None:
    assert normalize_test_id("") == ""
    assert normalize_test_id("   ") == ""


def test_id_path_and_node_split() -> None:
    assert id_path("tests/test_a.py::TestG::test_m") == "tests/test_a.py"
    assert id_node("tests/test_a.py::TestG::test_m") == "TestG::test_m"
    # 只有路径、没有节点
    assert id_path("tests/test_a.py") == "tests/test_a.py"
    assert id_node("tests/test_a.py") == ""


# ── junitxml 的 classname 还原 ──────────────────────────────


@pytest.mark.parametrize(
    ("shape", "classname", "name", "expected"),
    [
        ("模块级函数", "tests.test_a", "test_x", "tests/test_a.py::test_x"),
        ("多层目录", "tests.sub.test_a", "test_x", "tests/sub/test_a.py::test_x"),
        ("仓库根下的模块", "test_a", "test_x", "test_a.py::test_x"),
        (
            "类方法",
            "tests.test_a.TestGroup",
            "test_m",
            "tests/test_a.py::TestGroup::test_m",
        ),
        (
            "嵌套类",
            "tests.test_a.TestOuter.TestInner",
            "test_m",
            "tests/test_a.py::TestOuter::TestInner::test_m",
        ),
        (
            "参数化的类方法",
            "tests.test_a.TestGroup",
            "test_m[0]",
            "tests/test_a.py::TestGroup::test_m[0]",
        ),
    ],
)
def test_junit_case_id_splits_classname(
    shape: str, classname: str, name: str, expected: str
) -> None:
    """没有 `file` 属性（默认的 xunit2）时，按命名约定切 classname。

    `a.b.C` 可能是 `a/b/C.py`，也可能是 `a/b.py` 里的类 `C`。靠 pytest 的默认
    收集规则（模块叫 `test_*.py`、类以 `Test` 开头）挑最可能的那种。
    """
    assert junit_case_id(classname, name).primary == expected, shape


def test_junit_case_id_keeps_other_splits_as_alternates() -> None:
    """猜错的代价是一道题白判，所以其余切法全部留作备选。"""
    case = junit_case_id("tests.test_a.TestGroup", "test_m")
    assert case.ambiguous is True
    assert case.primary == "tests/test_a.py::TestGroup::test_m"
    # 另外两种切法：整个当模块路径，或者只有第一段是模块
    assert set(case.alternates) == {
        "tests/test_a/TestGroup.py::test_m",
        "tests.py::test_a::TestGroup::test_m",
    }


def test_junit_case_id_prefers_module_when_all_lowercase() -> None:
    """全小写的 classname 同分，此时优先当成模块路径。

    `pkg.sub.mod` 更可能是三层目录，而不是 `pkg/sub.py` 里一个叫 `mod` 的类。
    """
    assert junit_case_id("pkg.sub.mod", "test_x").primary == "pkg/sub/mod.py::test_x"


def test_junit_case_id_file_attribute_removes_ambiguity() -> None:
    """有 `file` 属性（xunit1 family）时路径照抄，不再有歧义，也不留备选。"""
    case = junit_case_id("tests.sub.test_a.TestGroup", "test_m", "tests/sub/test_a.py")
    assert case.primary == "tests/sub/test_a.py::TestGroup::test_m"
    assert case.alternates == ()
    assert case.ambiguous is False


def test_junit_case_id_file_attribute_wins_over_classname() -> None:
    """`file` 和 classname 对不上时（rootdir 不是仓库根），路径以 `file` 为准。

    `file` 是 pytest 直接写下来的事实，classname 是推导出来的。这时类名链只能靠猜，
    所以歧义标记要立起来、备选要留着。
    """
    case = junit_case_id("some.other.TestGroup", "test_m", "./tests/test_a.py")
    assert case.primary == "tests/test_a.py::TestGroup::test_m"
    assert case.ambiguous is True
    assert case.alternates


def test_junit_case_id_relativizes_file_with_repo_root() -> None:
    case = junit_case_id(
        "tests.test_a", "test_x", "/workspace/tests/test_a.py", repo_root="/workspace"
    )
    assert case.primary == "tests/test_a.py::test_x"


def test_junit_case_id_decodes_escaped_name() -> None:
    case = junit_case_id("tests.test_a", r"test_param[\u5e26]")
    assert case.primary == "tests/test_a.py::test_param[带]"


def test_junit_case_id_empty_classname_does_not_crash() -> None:
    """收集失败的条目 classname 是空的。调用方通常先摘出去了，这里保证不炸。"""
    assert junit_case_id("", "brk.test_broken").primary == "brk.test_broken"


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [
        ("brk.test_broken", "brk/test_broken.py"),
        ("test_broken", "test_broken.py"),
        ("a.b.c.test_d", "a/b/c/test_d.py"),
        # 已经是路径的原样归一化，不重复加后缀
        ("./brk/test_broken.py", "brk/test_broken.py"),
        ("", ""),
    ],
)
def test_module_path_from_dotted(dotted: str, expected: str) -> None:
    """收集失败的条目里 `name` 是点分模块名，要还原成文件路径。"""
    assert module_path_from_dotted(dotted) == expected
