"""录制测试报告 fixture（E4-T1）。

    python -m tests.fixtures.reports._record          # 重录全部 fixture
    python -m tests.fixtures.reports._record --check  # CI 用：录出来和仓库里的不一致就非零退出

## 为什么要有这个脚本，而不是手写 XML

手写的 XML 只会包含"我以为 pytest 会输出什么"。真实工具有一堆反直觉的怪癖，
手写永远想不全，而解析器一旦漏掉一种，后果是**大量假 MISSING**——看起来像被测 AI
作弊，其实是我们自己的解析器错了（协议 C-13a）。

这些怪癖是 2026-09-05 在开发机上用 pytest 9.1.1 实测出来的：

1. **`classname` 是点分模块路径，不是文件路径。** `tests/sub/test_nested.py` 记成
   `classname="tests.sub.test_nested"`。类方法把类名接在后面：
   `classname="tests.test_shapes.TestGroup" name="test_method"`。
   于是 `a.b.C` 有歧义——可能是 `a/b/C.py`，也可能是 `a/b.py` 里的类 `C`。
2. **默认的 `junit_family=xunit2` 没有 `file` 属性，`xunit1` 有。**
   xunit1 会写 `file="tests/sub/test_nested.py"`，歧义直接消失。所以两种都录。
3. **`<error>` 和 `<failure>` 不按直觉分。** 测试函数体里 `raise RuntimeError` 是
   `<failure>`；fixture / setup / teardown 里抛异常才是 `<error>`。
4. **XFAIL 认得出，非 strict 的 XPASS 认不出。** `xfail` 是
   `<skipped type="pytest.xfail">`，而 XPASS 在 XML 里就是一个没有子元素的普通
   testcase，和 PASSED 一模一样。`strict=True` 的 XPASS 是例外，它会写成
   `<failure message="[XPASS(strict)] ...">`。
5. **参数化用例里的非 ASCII 会被转义成字面的 `\\uXXXX`。**
   `test_param["带空格 的"]` 在 XML 里是 `name="test_param[\\u5e26\\u7a7a\\u683c \\u7684]"`。
6. **收集失败的 testcase 长得完全不一样**：`classname=""`、`name` 是点分模块名、
   `<error message="collection failure">`，pytest 退出码 2。
7. **文本短摘要里的 SKIPPED 行拿不到用例 ID**，只有 `文件:行号`。

## 录出来的 12 份

| 文件 | 覆盖什么 |
|:---|:---|
| `shapes_xunit2.xml` | 通过 / 断言失败 / 函数体抛异常 / setup 错误 / skip / xfail /
  xpass / strict xpass / 参数化（含非 ASCII）/ 类方法 / 嵌套目录 |
| `shapes_xunit1.xml` | 同一次运行的 xunit1 版本，带 `file` 属性 |
| `shapes_stdout.txt` | 同一次运行的 `-v -rA` 文本输出，文本兜底的主力 |
| `shapes_quiet_stdout.txt` | 默认 `-q` 输出：短摘要里只有失败和错误，15 条只捞得到 4 条 |
| `collection_error_xunit2.xml` | import 失败的收集错误 |
| `collection_error_stdout.txt` | 同上的文本输出 |
| `empty_xunit2.xml` | 一条用例都没收集到 |
| `truncated_xunit2.xml` | 容器被杀导致 XML 写了一半 |
| `golden_textkit_base_xunit2.xml` | **真实 Golden 题**在 base 上跑（F2P 挂） |
| `golden_textkit_base_stdout.txt` | 同上的文本输出 |
| `golden_textkit_fixed_xunit2.xml` | 同一道题打上 gold_patch 之后跑（全过） |
| `golden_textkit_fixed_xunit1.xml` | 同上的 xunit1 版本 |

Golden 那四份需要 `var/mirrors/` 里有镜像（先跑一次 `make golden`）。没有镜像时
本脚本会跳过它们并打印提示，不会失败——`--check` 模式下同样只跳过。

## 录出来的东西为什么是稳定的

`timestamp`、`hostname`、`time` 这三个属性每次跑都不一样，直接存进 fixture 会让
`--check` 永远红。这里把它们**替换成固定值**再落盘（见 `_stabilize_xml`），
文本输出里的耗时、路径、pytest 版本行同理（见 `_stabilize_text`）。

被替换掉的都是解析器**不看**的字段。`time` 是个例外：解析器要拿它算 `duration_ms`，
所以这里不抹掉它的值，只把它规整成两位小数，保证跨机器一致。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from cli.golden import (
    DEFAULT_MIRROR_ROOT,
    GOLDEN_ROOT,
    apply_patch,
    pytest_env,
)

FIXTURES_DIR = Path(__file__).resolve().parent

#: 录 fixture 用的那个小仓库。内容嵌在这里而不是单独建目录：这些文件只有本脚本用，
#: 摊成十几个真实文件之后，`make test` 会顺手把它们当测试收集进来。
SHAPES_SOURCES: dict[str, str] = {
    "tests/test_shapes.py": '''import pytest


def test_ok():
    assert True


def test_assert_fail():
    assert 1 == 2, "one is not two"


def test_raises_runtime():
    """函数体里抛异常 —— junitxml 记成 <failure>，不是 <error>。"""
    raise RuntimeError("boom in body")


@pytest.fixture
def broken_fixture():
    raise ValueError("fixture blew up")


def test_setup_error(broken_fixture):
    """fixture 里抛异常 —— 这才是 <error>。"""
    assert True


def test_skipped():
    pytest.skip("not today")


@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="already fixed")
def test_xpass():
    """非 strict 的 XPASS：XML 里和 PASSED 长得一模一样。"""
    assert True


@pytest.mark.xfail(reason="strict one", strict=True)
def test_xpass_strict():
    """strict 的 XPASS：XML 里是 <failure message="[XPASS(strict)] ...">。"""
    assert True


@pytest.mark.parametrize("value", [1, 2, "带空格 的"])
def test_param(value):
    assert value


class TestGroup:
    def test_method(self):
        assert True

    @pytest.mark.parametrize("n", [0, 1])
    def test_method_param(self, n):
        assert n >= 0
''',
    "tests/sub/test_nested.py": '''def test_deep():
    """多层目录：classname 是 tests.sub.test_nested。"""
    assert True
''',
}

BROKEN_SOURCES: dict[str, str] = {
    "brk/test_broken.py": """import nonexistent_module_xyz


def test_never():
    assert True
""",
}

#: 拿来录真实报告的 Golden 题。选 textkit 是因为它的 F2P 和 P2P 都不止一条，
#: 一份报告里就能同时看到"该挂的挂了"和"该过的过了"。
GOLDEN_TASK_ID = "bench-golden__textkit-1"

PYTEST_BASE_ARGS = ("-p", "no:cacheprovider", "-p", "no:randomly")

#: 会随机器和时刻变化、而解析器又不看的属性，一律替换成这些固定值。
_UNSTABLE_XML_ATTRS = {
    "timestamp": "2026-09-05T00:00:00+00:00",
    "hostname": "fixture-host",
}


def _write_sources(root: Path, sources: dict[str, str]) -> None:
    for rel, text in sources.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _run_pytest(cwd: Path, args: Sequence[str]) -> str:
    """在 `cwd` 里跑一次 pytest，返回合并后的 stdout+stderr。

    环境从 `cli.golden.pytest_env()` 拿 —— 那里是确定性设置的唯一出处。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_BASE_ARGS, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=pytest_env(cwd),
    )
    return completed.stdout + completed.stderr


def _stabilize_xml(text: str, cwd: Path) -> str:
    """抹掉 XML 里每次跑都不同、解析器又不看的东西。

    临时目录也要抹：`<skipped>` 的正文里写着触发 skip 的**绝对**路径，
    每次录制的临时目录都不一样，留着会让 `--check` 永远红。
    """
    text = text.replace(str(cwd), "/repo")
    for attr, value in _UNSTABLE_XML_ATTRS.items():
        text = re.sub(rf'{attr}="[^"]*"', f'{attr}="{value}"', text)
    # testsuite 上的总耗时归零；testcase 上的 time 留着（解析器要拿它算 duration_ms），
    # 但真实值是 0.000~0.099 的抖动，统一成两位小数才能跨机器一致
    text = re.sub(r'(<testsuite [^>]*?)time="[^"]*"', r'\1time="0.100"', text)
    text = re.sub(r'(<testcase [^>]*?)time="0\.\d+"', r'\1time="0.01"', text)
    return text.rstrip("\n") + "\n"


def _stabilize_text(text: str, cwd: Path) -> str:
    """抹掉文本输出里的机器相关信息：临时目录、python/pytest 版本、耗时。"""
    text = text.replace(str(cwd), "/repo")
    text = re.sub(r"^platform .*$", "platform linux -- Python 3.x, pytest-9.x", text, flags=re.M)
    text = re.sub(r"^rootdir: .*$", "rootdir: /repo", text, flags=re.M)
    text = re.sub(r"^plugins: .*$", "plugins: (stabilized)", text, flags=re.M)
    text = re.sub(r"^cachedir: .*$", "cachedir: (stabilized)", text, flags=re.M)
    text = re.sub(r"in \d+\.\d+s", "in 0.10s", text)
    text = re.sub(r"^/[^\s:]+/(importlib|_pytest)/", r"/py/\1/", text, flags=re.M)
    return text.rstrip("\n") + "\n"


def _record_shapes(work: Path) -> dict[str, str]:
    """一次运行覆盖十一种用例形态，录出四份 fixture。"""
    root = work / "shapes"
    root.mkdir()
    _write_sources(root, SHAPES_SOURCES)

    stdout = _run_pytest(root, ["--junitxml=x2.xml", "-o", "junit_family=xunit2", "-v", "-rA"])
    _run_pytest(root, ["--junitxml=x1.xml", "-o", "junit_family=xunit1"])
    quiet = _run_pytest(root, ["-q", "--no-header"])

    return {
        "shapes_xunit2.xml": _stabilize_xml((root / "x2.xml").read_text(encoding="utf-8"), root),
        "shapes_xunit1.xml": _stabilize_xml((root / "x1.xml").read_text(encoding="utf-8"), root),
        "shapes_stdout.txt": _stabilize_text(stdout, root),
        "shapes_quiet_stdout.txt": _stabilize_text(quiet, root),
    }


def _record_collection_error(work: Path) -> dict[str, str]:
    root = work / "broken"
    root.mkdir()
    _write_sources(root, BROKEN_SOURCES)
    stdout = _run_pytest(root, ["--junitxml=x2.xml", "-v", "-rA"])
    return {
        "collection_error_xunit2.xml": _stabilize_xml(
            (root / "x2.xml").read_text(encoding="utf-8"), root
        ),
        "collection_error_stdout.txt": _stabilize_text(stdout, root),
    }


def _record_empty(work: Path) -> dict[str, str]:
    root = work / "empty"
    root.mkdir()
    _run_pytest(root, ["--junitxml=x2.xml"])
    return {"empty_xunit2.xml": _stabilize_xml((root / "x2.xml").read_text(encoding="utf-8"), root)}


def _record_truncated(shapes_xml: str) -> dict[str, str]:
    """模拟"容器被杀，XML 只写了一半"。

    切断点选在一个 `<testcase>` 的中间，而不是两个 testcase 之间 —— 后者只要补上
    闭合标签就能解析，前者还要先退回到最后一个完整的 testcase。解析器两种都得能救。
    """
    cut = shapes_xml.index('<testcase classname="tests.test_shapes" name="test_xfail"')
    return {"truncated_xunit2.xml": shapes_xml[: cut + 40]}


def _record_golden(work: Path) -> dict[str, str]:
    """从真实 Golden 题录两轮：base（F2P 挂）和 base+gold_patch（全过）。

    没有镜像就返回空 dict，由调用方打印提示 —— `var/mirrors/` 在 .gitignore 里，
    换台机器 clone 下来是空的，不能因此让录制脚本失败。
    """
    from app.benchmark.schema import TaskDefinition
    from app.sandbox.mirror import MirrorManager
    from app.sandbox.workspace import materialize_workspace

    task = TaskDefinition.model_validate_json(
        (GOLDEN_ROOT / f"{GOLDEN_TASK_ID}.json").read_text(encoding="utf-8")
    )
    mirrors = MirrorManager(DEFAULT_MIRROR_ROOT)
    if not mirrors.exists(task.repo_name):
        return {}
    mirror = mirrors.path_for(task.repo_name)
    cases = [*task.fail_to_pass, *task.pass_to_pass]

    out: dict[str, str] = {}

    base_ws = materialize_workspace(
        mirror_path=mirror, base_commit=task.base_commit, dest=work / "golden-base"
    )
    apply_patch(base_ws, work / "test.patch", task.test_patch)
    base_stdout = _run_pytest(base_ws.path, ["--junitxml=report/junit.xml", "-v", "-rA", *cases])
    out["golden_textkit_base_xunit2.xml"] = _stabilize_xml(
        (base_ws.path / "report" / "junit.xml").read_text(encoding="utf-8"), base_ws.path
    )
    out["golden_textkit_base_stdout.txt"] = _stabilize_text(base_stdout, base_ws.path)

    fixed_ws = materialize_workspace(
        mirror_path=mirror, base_commit=task.base_commit, dest=work / "golden-fixed"
    )
    apply_patch(fixed_ws, work / "gold.patch", task.gold_patch)
    apply_patch(fixed_ws, work / "test2.patch", task.test_patch)
    _run_pytest(fixed_ws.path, ["--junitxml=report/junit.xml", *cases])
    out["golden_textkit_fixed_xunit2.xml"] = _stabilize_xml(
        (fixed_ws.path / "report" / "junit.xml").read_text(encoding="utf-8"), fixed_ws.path
    )
    _run_pytest(
        fixed_ws.path,
        ["--junitxml=report/junit1.xml", "-o", "junit_family=xunit1", *cases],
    )
    out["golden_textkit_fixed_xunit1.xml"] = _stabilize_xml(
        (fixed_ws.path / "report" / "junit1.xml").read_text(encoding="utf-8"), fixed_ws.path
    )
    return out


def record() -> dict[str, str]:
    """跑一遍全部录制，返回 {文件名: 内容}。"""
    with tempfile.TemporaryDirectory(prefix="record-reports-") as tmp:
        work = Path(tmp)
        recorded = _record_shapes(work)
        recorded.update(_record_collection_error(work))
        recorded.update(_record_empty(work))
        recorded.update(_record_truncated(recorded["shapes_xunit2.xml"]))
        golden = _record_golden(work)
        if golden:
            recorded.update(golden)
        else:
            print(f"跳过 Golden 报告：{DEFAULT_MIRROR_ROOT} 里没有镜像，先跑 `make golden`")
    return recorded


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="录制测试报告解析器的 fixture")
    parser.add_argument("--check", action="store_true", help="只比对，不写盘；不一致则非零退出")
    args = parser.parse_args(argv)

    recorded = record()
    if not args.check:
        for name, text in sorted(recorded.items()):
            (FIXTURES_DIR / name).write_text(text, encoding="utf-8")
        print(f"已写入 {len(recorded)} 份 fixture 到 {FIXTURES_DIR}")
        return 0

    drifted = [
        name
        for name, text in sorted(recorded.items())
        if not (FIXTURES_DIR / name).exists()
        or (FIXTURES_DIR / name).read_text(encoding="utf-8") != text
    ]
    if drifted:
        print("这些 fixture 和重新录出来的不一致：" + "、".join(drifted))
        return 1
    print(f"{len(recorded)} 份 fixture 与重新录制的结果一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
