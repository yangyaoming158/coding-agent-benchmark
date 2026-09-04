"""受保护路径规则的测试（协议 C-42、C-61、C-75、C-76）。

这些规则是防作弊的地基：被测 AI 改了受保护文件，那部分改动一律丢掉。
规则漏一条，AI 把测试改成 `assert True` 就"通过"了。
"""

from __future__ import annotations

import pytest

from app.domain.protected_paths import (
    DEFAULT_PROTECTED_PATTERNS,
    agent_visible_patterns,
    enforcement_patterns,
    is_protected,
    normalize_path,
    protected_hits,
)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "test/test_a.py",
        "src/pkg/tests/test_a.py",
        "src/pkg/test/helper.py",
        "test_root_level.py",
        "src/module_test.py",
        "src/conftest.py",
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "src/sitecustomize.py",
        "usercustomize.py",
        ".github/workflows/ci.yml",
    ],
)
def test_default_patterns_cover_protocol_list(path: str) -> None:
    """C-42 清单里的每一类都要真的匹配上。

    `pyproject.toml` 是 v1.0 漏掉后补上的：它的 `[tool.pytest.*]` 段落能改变
    测试行为，只保护那一段做不到，简化成整文件保护。
    """
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "nonebot/adapter.py", "README.md", "docs/latest.md", "setup.py"],
)
def test_normal_source_files_are_not_protected(path: str) -> None:
    """正常源码不能被误伤。

    误伤的后果很实际：修 bug 时新建一个模块是完全正常的，
    把它当受保护文件丢掉，等于把正确答案删了（协议 C-63a 讲的就是这件事）。
    """
    assert not is_protected(path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("./tests/test_a.py", "tests/test_a.py"),
        ("tests\\test_a.py", "tests/test_a.py"),
        ("tests//test_a.py", "tests/test_a.py"),
        ("/tests/test_a.py", "tests/test_a.py"),
        ("tests/./test_a.py", "tests/test_a.py"),
        ("  tests/test_a.py  ", "tests/test_a.py"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    """路径写法不同但指的是同一个文件，必须归到一起（C-74 第 2 条）。

    不归一化的话 `./tests/x.py` 匹配不上 `tests/**`，防线静默失效——
    这和用例 ID 归一化是同一类坑（AGENTS.md 第 5.5 条）。
    """
    assert normalize_path(raw) == expected


def test_unnormalized_path_still_matches() -> None:
    """`is_protected` 自己会先归一化，调用方不用记得先转一次。"""
    assert is_protected("./tests/test_a.py")
    assert is_protected("tests\\test_a.py")


# ── 两份清单不能混用（C-75、C-76）────────────────────────────


def test_agent_visible_list_excludes_test_patch_paths() -> None:
    """下发给被测 AI 的清单里，绝不能有该题的 `test_patch_paths`（C-76）。

    把官方测试补丁改了哪几个文件告诉 AI，等于给了定位提示。
    我们连 F2P 的用例 ID 都没下发，不能从这个字段漏出去。
    """
    secret = "tests/fixtures/reconnect.json"
    visible = agent_visible_patterns()
    enforcement = enforcement_patterns(test_patch_paths=(secret,))

    assert secret not in visible
    assert secret in enforcement


def test_environment_extra_paths_are_appended_not_replaced() -> None:
    """环境规格只能在默认清单上追加，不能替换（C-61）。

    允许替换的话，某个仓库配错一次，防作弊就整体失效，而且不会有任何报错。
    """
    extended = enforcement_patterns(extra=("vendor/**",))
    assert set(DEFAULT_PROTECTED_PATTERNS) <= set(extended)
    assert "vendor/**" in extended


def test_extra_paths_are_visible_to_agent() -> None:
    """环境追加的路径是仓库级通用规则，可以下发——它们不泄露单道题的信息。"""
    assert "vendor/**" in agent_visible_patterns(extra=("vendor/**",))


def test_protected_hits_reports_sorted_unique() -> None:
    """报错要说清楚碰了哪几个受保护文件，排序去重，方便直接贴进 issue。"""
    hits = protected_hits(
        ("./tests/test_b.py", "tests/test_b.py", "src/app.py", "conftest.py"),
        DEFAULT_PROTECTED_PATTERNS,
    )
    assert hits == ["conftest.py", "tests/test_b.py"]
