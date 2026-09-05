"""在真工作区上抓补丁、归一化、再打回去（E3-T3 的验收标准）。

这一组真的物化工作区、真的改文件、真的跑 `git apply --3way`。上一层
（`tests/unit/test_patch_normalization.py`）只验补丁字符串怎么被切；
这里验的是**切完之后还能不能用**。

任务卡三条验收标准在这里各有对应的用例：

1. Agent 改测试文件时该改动被剔除 → `test_cheating_edit_is_dropped_...`
2. `__pycache__` / `.aider*` 被忽略   → `test_noise_never_reaches_the_patch`
3. 输出可 `git apply --3way`         → `test_normalized_patch_applies_three_way`
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.protected_paths import enforcement_patterns
from app.runner.patch import capture_workspace_diff, normalize_patch, write_patch
from app.sandbox.git_cli import run_git
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks

TASK = load_tasks()[0]
ENFORCEMENT = enforcement_patterns(tuple(TASK.test_patch_paths))

#: Agent 会去改的那个源文件（这道题的被测代码）。
SOURCE_FILE = "auth/password.py"
#: Agent 顺手新建的源文件。修 bug 时新建模块是正常行为，它必须活下来。
NEW_SOURCE_FILE = "auth/policy.py"
#: Agent 试图改的测试文件 —— 这就是要被剔除的那一份。
CHEAT_FILE = "tests/test_password.py"


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """现建一套 golden 镜像，不用开发机上 `var/mirrors` 里那份。"""
    root = tmp_path_factory.mktemp("patch-capture-mirrors")
    build(mirror_root=root)
    return root


def make_workspace(mirror_root: Path, dest: Path) -> Workspace:
    return materialize_workspace(
        mirror_path=MirrorManager(mirror_root).path_for(TASK.repo_name),
        base_commit=TASK.base_commit,
        dest=dest,
    )


def act_like_a_cheating_agent(workspace: Workspace) -> None:
    """模拟一个"改了源码、也改了测试、还掉了一地碎屑"的 Agent。

    四件事各有用意：改源码是真修复、新建源文件是合法行为、改测试是作弊、
    剩下两个是干活掉下来的垃圾。归一化之后应该只剩前两件。
    """
    root = workspace.path
    source = root / SOURCE_FILE
    source.write_text(source.read_text(encoding="utf-8") + "\n# agent 动过这里\n", encoding="utf-8")
    (root / NEW_SOURCE_FILE).write_text("MIN_LENGTH = 8\n", encoding="utf-8")
    (root / CHEAT_FILE).write_text("def test_everything_passes():\n    assert True\n", "utf-8")

    (root / "auth" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (root / "auth" / "__pycache__" / "password.cpython-311.pyc").write_bytes(b"\x00fake bytecode")
    (root / ".aider.chat.history.md").write_text("# 聊天记录\n", encoding="utf-8")


# ── 捕获 ────────────────────────────────────────────────────


def test_capture_sees_modified_and_newly_added_files(mirror_root: Path, tmp_path: Path) -> None:
    """新建的源文件必须进补丁。

    漏掉它就是把 AI 的修复删了一半 —— 而且不报错，只会判成"没修好"。
    `git diff` 默认看不到未跟踪文件，所以捕获前必须先 `git add`。
    """
    workspace = make_workspace(mirror_root, tmp_path / "agent")
    act_like_a_cheating_agent(workspace)

    raw = capture_workspace_diff(workspace)

    assert SOURCE_FILE in raw
    assert NEW_SOURCE_FILE in raw, "新建的源文件没进补丁"
    assert CHEAT_FILE in raw, "作弊的改动要留在原始补丁里当证据（协议 C-08b）"


def test_noise_never_reaches_the_patch(mirror_root: Path, tmp_path: Path) -> None:
    """任务卡第二条验收：`__pycache__` / `.aider*` 被忽略。

    这里挡住它们的是工作区的 `.git/info/exclude`（E2-T1 写的），`git add -A`
    根本不会把它们暂存进去。归一化那边还有第二道，走 strict 模式的适配器
    （AI 自己打印 diff、不经过 git）要靠那一道。
    """
    workspace = make_workspace(mirror_root, tmp_path / "agent")
    act_like_a_cheating_agent(workspace)

    raw = capture_workspace_diff(workspace)

    assert "__pycache__" not in raw
    assert ".aider" not in raw


def test_capture_works_after_the_agent_commits(mirror_root: Path, tmp_path: Path) -> None:
    """有些 Agent 干完活会自己 `git commit`，那时裸 `git diff` 是空的。

    基准取 `base_sha` 就不受影响。不这么做的话，这类 Agent 会全部被判成
    "什么都没交"，而它其实做对了。
    """
    workspace = make_workspace(mirror_root, tmp_path / "agent")
    act_like_a_cheating_agent(workspace)
    run_git(["add", "--all", "--", "."], cwd=workspace.path)
    run_git(
        [
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            "agent 自己提交了",
        ],
        cwd=workspace.path,
    )

    assert run_git(["diff"], cwd=workspace.path).stdout == "", "裸 diff 确实是空的"
    assert SOURCE_FILE in capture_workspace_diff(workspace)


# ── 归一化 + 打回去 ─────────────────────────────────────────


def test_cheating_edit_is_dropped_and_real_work_survives(mirror_root: Path, tmp_path: Path) -> None:
    """任务卡第一条验收：改测试文件的那一段被剔除，源码改动一个不少。"""
    workspace = make_workspace(mirror_root, tmp_path / "agent")
    act_like_a_cheating_agent(workspace)

    result = normalize_patch(capture_workspace_diff(workspace), protected_patterns=ENFORCEMENT)

    assert CHEAT_FILE not in result.text
    assert SOURCE_FILE in result.text
    assert NEW_SOURCE_FILE in result.text
    assert result.protected_path_edit_attempted
    assert result.stats.files_changed == 2
    assert result.raw_stats.files_changed == 3


def test_normalized_patch_applies_three_way(mirror_root: Path, tmp_path: Path) -> None:
    """任务卡第三条验收：标准化补丁能用 `git apply --3way` 打到一份干净工作区上。

    打的是**新物化的一份**，不是 Agent 那份（协议 C-15）。这也是判定链第 1、2 步
    真正会走的路，所以这条用例等于把 E4-T2 的前半段先验了一遍。
    """
    agent_ws = make_workspace(mirror_root, tmp_path / "agent")
    act_like_a_cheating_agent(agent_ws)
    result = normalize_patch(capture_workspace_diff(agent_ws), protected_patterns=ENFORCEMENT)

    test_ws = make_workspace(mirror_root, tmp_path / "test")
    patch_file = write_patch(result.text, tmp_path / "normalized.patch")
    applied = run_git(
        ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
        cwd=test_ws.path,
        check=False,
    )

    assert applied.returncode == 0, f"标准化补丁打不上：{applied.stderr}"
    assert "# agent 动过这里" in (test_ws.path / SOURCE_FILE).read_text(encoding="utf-8")
    assert (test_ws.path / NEW_SOURCE_FILE).exists(), "新建的源文件要跟着落地"
    assert "assert True" not in (test_ws.path / CHEAT_FILE).read_text(encoding="utf-8"), (
        "作弊的测试改动不该出现在测试工作区里"
    )


def test_an_agent_that_only_cheats_produces_an_empty_patch(
    mirror_root: Path, tmp_path: Path
) -> None:
    """只改测试文件 → 标准化后为空，但两个诊断字段留下了证据（协议 C-08a、C-08b）。"""
    workspace = make_workspace(mirror_root, tmp_path / "agent")
    (workspace.path / CHEAT_FILE).write_text("def test_x():\n    assert True\n", encoding="utf-8")

    result = normalize_patch(capture_workspace_diff(workspace), protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert not result.raw_patch_empty
    assert result.protected_path_edit_attempted


def test_an_idle_agent_produces_an_empty_patch_with_no_evidence(
    mirror_root: Path, tmp_path: Path
) -> None:
    """什么都没干：补丁空、原始补丁也空、没有作弊痕迹。和上一条对照着看。"""
    workspace = make_workspace(mirror_root, tmp_path / "agent")

    result = normalize_patch(capture_workspace_diff(workspace), protected_patterns=ENFORCEMENT)

    assert result.is_empty
    assert result.raw_patch_empty
    assert not result.protected_path_edit_attempted


def test_deleted_files_survive_normalization(mirror_root: Path, tmp_path: Path) -> None:
    """删文件也是合法的修复动作，补丁要带得动，也要打得上。"""
    agent_ws = make_workspace(mirror_root, tmp_path / "agent")
    (agent_ws.path / "README.md").unlink()

    result = normalize_patch(capture_workspace_diff(agent_ws), protected_patterns=ENFORCEMENT)

    test_ws = make_workspace(mirror_root, tmp_path / "test")
    patch_file = write_patch(result.text, tmp_path / "delete.patch")
    applied = run_git(
        ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
        cwd=test_ws.path,
        check=False,
    )

    assert applied.returncode == 0, applied.stderr
    assert not (test_ws.path / "README.md").exists()
