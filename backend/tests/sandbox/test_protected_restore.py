"""防作弊第二道防线的单测（E4-T2，协议 C-16、C-63、C-63a、C-63b）。

这些用例不需要 Docker —— 强制还原是纯 git 操作，跑一遍几百毫秒，进每次提交的快速集。
容器里的端到端验证在 `test_execute_tests.py`（带 docker 标记）。

**这一层挡的是什么**：被测 AI 把 `tests/test_a.py` 改成 `assert True` 就"通过"了。
生成补丁时（E3-T3）已经按路径过滤过一遍，这里再强制还原一遍是第二道独立的防线 ——
两处实现只要有一处写出 bug，基准都不会被攻破（C-16）。

**这一层不能挡过头**：修 bug 时新建一个模块文件是完全正常的行为。
`git clean -fd` 会把它一起删掉，等于把正确答案删了，所以 C-63a 明令禁止。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.enums import InfraOutcome
from app.domain.execution_plan import ExecutionPlan
from app.domain.protected_paths import enforcement_patterns
from app.evaluation.executor import execute_tests, restore_protected_paths
from app.sandbox.container import ContainerResult, ContainerSpec, NetworkMode, Stage
from app.sandbox.git_cli import run_git
from app.sandbox.workspace import Workspace, materialize_workspace
from tests.sandbox.conftest import SourceRepo, commit_all, git, write

#: 这道"题"的 test_patch 碰过的路径。里面那个 JSON 的名字完全不像测试文件，
#: 靠通配符匹配不到 —— C-42 最后一条要求把它并进受保护清单，这里就是那种情况。
TEST_PATCH_PATHS = ("tests/test_app.py", "tests/fixtures/reconnect.json")

PATTERNS = enforcement_patterns(TEST_PATCH_PATHS)


@pytest.fixture
def workspace(source_repo: SourceRepo, mirror_of: Path, tmp_path: Path) -> Workspace:
    """一个物化好的干净工作区，停在 base commit。"""
    return materialize_workspace(
        mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=tmp_path / "ws"
    )


def read(workspace: Workspace, relative: str) -> str:
    return (workspace.path / relative).read_text(encoding="utf-8")


# ── 该挡的要挡住 ────────────────────────────────────────────


def test_modified_test_file_is_restored(workspace: Workspace) -> None:
    """AI 把测试改成永远通过 —— 必须被还原成 base 的样子。"""
    original = read(workspace, "tests/test_app.py")
    (workspace.path / "tests/test_app.py").write_text(
        "def test_login():\n    assert True  # 我改的\n", encoding="utf-8"
    )

    restore = restore_protected_paths(workspace, PATTERNS)

    assert read(workspace, "tests/test_app.py") == original
    assert restore.restored == ("tests/test_app.py",)
    assert restore.attempted is True


def test_deleted_test_file_is_restored(workspace: Workspace) -> None:
    """AI 干脆把测试文件删了 —— 也要还原回来。

    删掉之后 pytest 收集不到那条用例，报告里就没有它，判定引擎会记 MISSING。
    不还原的话，"AI 删了测试"和"我们的解析器坏了"在数据上长得一模一样。
    """
    (workspace.path / "tests/test_app.py").unlink()

    restore = restore_protected_paths(workspace, PATTERNS)

    assert (workspace.path / "tests/test_app.py").is_file()
    assert restore.restored == ("tests/test_app.py",)


def test_added_conftest_is_deleted(workspace: Workspace) -> None:
    """AI 新建一个 `conftest.py` 做猴子补丁 —— 必须被删掉（C-63）。

    `git checkout` 只管已跟踪的文件，删不掉新建的未跟踪文件。少了这一半，
    第二道防线等于没有：AI 不改任何现有测试，加一个 conftest 就能改变收集行为。
    """
    (workspace.path / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n    items.clear()\n", encoding="utf-8"
    )

    restore = restore_protected_paths(workspace, PATTERNS)

    assert not (workspace.path / "conftest.py").exists()
    assert restore.deleted == ("conftest.py",)
    assert restore.attempted is True


def test_added_test_file_in_protected_dir_is_deleted(workspace: Workspace) -> None:
    """AI 往 `tests/` 里新塞一个文件 —— 同样删掉。"""
    new_test = workspace.path / "tests/test_extra.py"
    new_test.write_text("def test_x():\n    pass\n", encoding="utf-8")

    restore = restore_protected_paths(workspace, PATTERNS)

    assert not (workspace.path / "tests/test_extra.py").exists()
    assert restore.deleted == ("tests/test_extra.py",)


def test_ignored_files_under_protected_dir_are_deleted(workspace: Workspace) -> None:
    """被 .gitignore 挡住的新文件也要能删到（`include_ignored=True`）。

    基线忽略清单里有 `__pycache__/`，不带这个参数就列不出
    `tests/__pycache__/conftest.cpython-311.pyc`，也就删不掉 —— 而一个 .pyc
    足以让 Python import 到别的东西。
    """
    cache = workspace.path / "tests/__pycache__"
    cache.mkdir(parents=True)
    (cache / "conftest.cpython-311.pyc").write_bytes(b"\x00fake")

    restore = restore_protected_paths(workspace, PATTERNS)

    assert not (cache / "conftest.cpython-311.pyc").exists()
    assert restore.deleted == ("tests/__pycache__/conftest.cpython-311.pyc",)


def test_test_patch_paths_are_protected_even_without_a_matching_pattern(
    source_repo: SourceRepo, tmp_path: Path
) -> None:
    """名字完全不像测试的数据文件，靠 `test_patch_paths` 才受保护（C-42 最后一条）。

    `tests/fixtures/reconnect.json` 命中的是 `tests/**`，但真实题目里这种文件
    常常在 `tests/` 之外。这条用例把它放到 `data/` 下，通配符一个都匹配不到，
    只有 `test_patch_paths` 认得它 —— 漏掉它，AI 改了就生效，解决率静悄悄偏高。
    """
    write(source_repo.path, "data/reconnect.json", '{"retries": 3}\n')
    base = commit_all(source_repo.path, "test: 加一份测试数据")
    mirror = tmp_path / "m.git"
    git(source_repo.path, "clone", "--mirror", "--quiet", "--", str(source_repo.path), str(mirror))
    ws = materialize_workspace(mirror_path=mirror, base_commit=base, dest=tmp_path / "ws2")

    (ws.path / "data/reconnect.json").write_text('{"retries": 999}\n', encoding="utf-8")

    # 通配符清单认不出它
    assert restore_protected_paths(ws, enforcement_patterns()).attempted is False
    (ws.path / "data/reconnect.json").write_text('{"retries": 999}\n', encoding="utf-8")
    # 带上 test_patch_paths 就认得出
    restore = restore_protected_paths(ws, enforcement_patterns(("data/reconnect.json",)))
    assert restore.restored == ("data/reconnect.json",)
    assert (ws.path / "data/reconnect.json").read_text(encoding="utf-8") == '{"retries": 3}\n'


# ── 不能挡过头 ──────────────────────────────────────────────


def test_new_source_file_survives(workspace: Workspace) -> None:
    """AI 新建一个源码文件 —— 必须留着（C-63a）。

    修 bug 时新建模块是完全正常的行为。`git clean -fd` 会把它删掉，
    等于把正确答案删了，然后判定说"没修好"，而 AI 其实修对了。
    """
    (workspace.path / "src/newmod.py").write_text("VALUE = 1\n", encoding="utf-8")

    restore = restore_protected_paths(workspace, PATTERNS)

    assert (workspace.path / "src/newmod.py").is_file()
    assert restore.deleted == ()


def test_modified_source_file_survives(workspace: Workspace) -> None:
    """AI 改源码 —— 那正是它该干的事，不能被还原掉。"""
    (workspace.path / "src/app.py").write_text("def login(u, p):\n    return bool(p)\n", "utf-8")

    restore = restore_protected_paths(workspace, PATTERNS)

    assert "return bool(p)" in read(workspace, "src/app.py")
    assert restore.restored == ()
    assert restore.attempted is False


def test_untouched_workspace_reports_nothing(workspace: Workspace) -> None:
    """AI 什么都没碰时，`attempted` 必须是 False。

    这个字段会触发人工复核（C-13d），误报一次就是一条白跑的复核任务。
    """
    restore = restore_protected_paths(workspace, PATTERNS)
    assert restore == restore.__class__()
    assert restore.attempted is False


# ── 确定性 ──────────────────────────────────────────────────


def test_restore_is_idempotent(workspace: Workspace) -> None:
    """还原两次和还原一次结果一样。

    第二次必须报"什么都没动" —— 如果它每次都报还原了点什么，
    `protected_path_edit_attempted` 就永远是 True，人工复核队列会被灌满。
    """
    (workspace.path / "tests/test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (workspace.path / "conftest.py").write_text("# 猴子补丁\n", encoding="utf-8")

    first = restore_protected_paths(workspace, PATTERNS)
    second = restore_protected_paths(workspace, PATTERNS)

    assert first.attempted is True
    assert second.attempted is False


def test_restore_lists_are_sorted(workspace: Workspace) -> None:
    """清单要排序 —— 它会进制品和复核任务，顺序抖动会让两次运行的 diff 假阳性。"""
    for name in ("tests/test_b.py", "tests/test_a.py", "conftest.py"):
        (workspace.path / name).write_text("# x\n", encoding="utf-8")

    restore = restore_protected_paths(workspace, PATTERNS)

    assert list(restore.deleted) == sorted(restore.deleted)
    assert list(restore.restored) == sorted(restore.restored)


# ── 打补丁和还原之间的接缝 ──────────────────────────────────


def _fake_container(exit_code: int = 0) -> ContainerResult:
    """假的容器结果。这一组用例只关心第 1–4 步，不需要真起容器。"""
    return ContainerResult(
        container_id="fake",
        image="fake:latest",
        exit_code=exit_code,
        oom_killed=False,
        timed_out=False,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def _plan(source_repo: SourceRepo, **overrides: object) -> ExecutionPlan:
    base = {
        "base_commit": source_repo.base_commit,
        "test_patch": "",
        "test_patch_paths": TEST_PATCH_PATHS,
        "fail_to_pass": ("tests/test_app.py::test_login",),
        "pass_to_pass": (),
        "test_command": "python -m pytest",
        "test_report_path": "report/junit.xml",
    }
    return ExecutionPlan(**{**base, **overrides})  # type: ignore[arg-type]


def _patch_adding(path: str, content: str, workspace: Workspace) -> str:
    """在一个临时工作区里造一段"新增某个文件"的补丁。

    必须走 `git diff` 生成、再走 `execute_tests` 打上 —— 手工把文件写进工作区
    绕过了 `git apply`，而下面那个坑恰恰只在 `git apply` 这条路上出现。
    """
    target = workspace.path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(["add", "--all"], cwd=workspace.path)
    diff = run_git(["diff", "--cached", workspace.base_sha], cwd=workspace.path).stdout
    run_git(["reset", "--quiet", "--hard", workspace.base_sha], cwd=workspace.path)
    return diff + "\n"


def test_patch_added_protected_file_is_deleted(
    source_repo: SourceRepo, mirror_of: Path, workspace: Workspace, tmp_path: Path
) -> None:
    """补丁**新增**一个受保护文件时，还原那一步不能崩（回归用例）。

    坑在这儿：`git apply --3way` 走三方合并时会把结果暂存进索引，新增的
    `conftest.py` 就成了"已暂存的新增文件"。`git diff --name-only HEAD` 会列出它，
    可它在 HEAD 里根本不存在，`git checkout HEAD -- conftest.py` 当场报
    "pathspec did not match any file(s) known to git" —— 整条防作弊防线崩在这里。

    直接把文件写进工作区**测不出**这个坑（那样它只是个未跟踪文件），
    必须真的走一遍 `git apply`。修法见 `_apply_patch` 末尾的 `git reset`。
    """
    patch = _patch_adding("conftest.py", "# 猴子补丁\n", workspace)

    outcome = execute_tests(
        _plan(source_repo),
        patch,
        mirror_path=mirror_of,
        workspace_dir=tmp_path / "exec-ws",
        run_container=lambda _spec: _fake_container(),
    )

    assert outcome.infra_outcome is InfraOutcome.SUCCESS
    assert outcome.restore.deleted == ("conftest.py",)
    assert not (tmp_path / "exec-ws" / "conftest.py").exists()


def test_patch_added_source_file_survives_apply(
    source_repo: SourceRepo, mirror_of: Path, workspace: Workspace, tmp_path: Path
) -> None:
    """同一条路上新增的**源码**文件必须活下来（C-63a）。"""
    patch = _patch_adding("src/newmod.py", "VALUE = 1\n", workspace)

    outcome = execute_tests(
        _plan(source_repo),
        patch,
        mirror_path=mirror_of,
        workspace_dir=tmp_path / "exec-ws2",
        run_container=lambda _spec: _fake_container(),
    )

    assert outcome.restore.deleted == ()
    kept = tmp_path / "exec-ws2" / "src" / "newmod.py"
    assert kept.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_unapplicable_agent_patch_maps_to_patch_apply_failed(
    source_repo: SourceRepo, mirror_of: Path, tmp_path: Path
) -> None:
    """补丁打不上是 `PATCH_APPLY_FAILED`，而且不该起容器。"""
    started: list[object] = []
    outcome = execute_tests(
        _plan(source_repo),
        "diff --git a/no/such.py b/no/such.py\n--- a/no/such.py\n+++ b/no/such.py\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
        mirror_path=mirror_of,
        workspace_dir=tmp_path / "exec-ws3",
        run_container=lambda spec: (started.append(spec), _fake_container())[1],
    )

    assert outcome.infra_outcome is InfraOutcome.PATCH_APPLY_FAILED
    assert started == [], "补丁都没打上，不该起容器"


def test_container_spec_has_network_off_and_task_limits(
    source_repo: SourceRepo, mirror_of: Path, tmp_path: Path
) -> None:
    """测试容器必须断网（协议 C-31），资源限额要来自题目。

    断网这条是硬性要求：`AGENT_RUNNING` 是唯一允许联网的阶段。断网还顺带挡住了
    一类作弊 —— 测试跑起来之后再去网上抓答案。
    """
    seen: list[ContainerSpec] = []
    execute_tests(
        _plan(source_repo, sandbox_cpu=2.0, sandbox_memory_mb=999, test_timeout_s=77),
        "",
        mirror_path=mirror_of,
        workspace_dir=tmp_path / "exec-ws4",
        run_container=lambda spec: (seen.append(spec), _fake_container())[1],
    )

    assert len(seen) == 1
    spec = seen[0]
    assert spec.network is NetworkMode.NONE
    assert spec.stage is Stage.TEST
    assert spec.limits.cpus == 2.0
    assert spec.limits.memory_mb == 999
    assert spec.timeout_s == 77


def test_only_f2p_and_p2p_ids_are_passed_to_pytest(
    source_repo: SourceRepo, mirror_of: Path, tmp_path: Path
) -> None:
    """只把 F2P + P2P 的用例 ID 接在命令后面（C-17），且去重。"""
    seen: list[ContainerSpec] = []
    execute_tests(
        _plan(
            source_repo,
            fail_to_pass=("tests/test_app.py::test_login",),
            pass_to_pass=("tests/test_app.py::test_login", "tests/test_app.py::test_other"),
        ),
        "",
        mirror_path=mirror_of,
        workspace_dir=tmp_path / "exec-ws5",
        run_container=lambda spec: (seen.append(spec), _fake_container())[1],
    )

    assert seen[0].command == [
        "python",
        "-m",
        "pytest",
        "tests/test_app.py::test_login",
        "tests/test_app.py::test_other",
    ]
