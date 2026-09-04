"""工作区物化与防泄题（E2-T1）。

三条验收标准在这里各有一组用例：

1. 物化后 `git log --oneline | wc -l == 1`     → `test_history_*`
2. `git log --all` 看不到 base 之后的提交       → `test_leak_*`
3. 两次物化同一 commit 的目录树哈希一致          → `test_determinism_*`

其余用例覆盖的是"物化出来的东西对不对"：内容等于 base 树、权限位没丢、
基线忽略清单挡对了东西、坏输入被拦住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.git_cli import run_git
from app.sandbox.mirror import MirrorError
from app.sandbox.workspace import (
    DEFAULT_WORKSPACE_IGNORE,
    Workspace,
    WorkspaceError,
    materialize_workspace,
    remove_workspace,
)
from tests.sandbox.conftest import (
    BUGGY_SOURCE,
    FIX_COMMIT_MESSAGE,
    FIXED_SOURCE,
    SourceRepo,
    commit_all,
    git,
    is_executable,
    write,
)


@pytest.fixture
def workspace(mirror_of: Path, source_repo: SourceRepo, tmp_path: Path) -> Workspace:
    """物化一份 base 状态的工作区，绝大多数用例的起点。"""
    return materialize_workspace(
        mirror_path=mirror_of,
        base_commit=source_repo.base_commit,
        dest=tmp_path / "ws" / "agent",
    )


# ── 验收标准 1：历史只剩一个提交 ────────────────────────────


def test_history_has_exactly_one_commit(workspace: Workspace) -> None:
    """`git log --oneline | wc -l == 1`。"""
    log = git(workspace.path, "log", "--oneline")
    assert len(log.splitlines()) == 1
    assert workspace.commit_count() == 1


def test_history_commit_has_no_parent(workspace: Workspace) -> None:
    """那唯一的提交是根提交 —— 没有父提交，也就无从往回翻。"""
    parents = git(workspace.path, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents == [workspace.base_sha]


def test_history_has_no_remote(workspace: Workspace) -> None:
    """没有 remote。有 remote 就有 `git fetch`，防泄题只剩一个网络故障的距离。"""
    assert git(workspace.path, "remote") == ""


# ── 验收标准 2：base 之后的历史一点都看不到 ──────────────────


def test_leak_all_refs_show_only_base_commit(workspace: Workspace, source_repo: SourceRepo) -> None:
    """`git log --all` 里只有我们自己建的那个提交，官方修复的提交信息不在其中。"""
    log_all = git(workspace.path, "log", "--all", "--oneline")
    assert len(log_all.splitlines()) == 1
    assert FIX_COMMIT_MESSAGE not in log_all
    assert "CHANGELOG" not in log_all


def test_leak_future_commit_objects_are_absent(
    workspace: Workspace, source_repo: SourceRepo
) -> None:
    """修复提交的对象根本不在工作区的 git 数据库里。

    这一条比"log 里看不到"强得多：即使有人手里攥着那个 SHA，
    在工作区里也 `git show` 不出来 —— 对象压根没被复制过来。
    """
    for sha in (source_repo.fix_commit, source_repo.later_commit):
        result = run_git(["cat-file", "-e", sha], cwd=workspace.path, check=False, timeout_s=60)
        assert result.returncode != 0, f"工作区里居然能读到 base 之后的提交 {sha}"


def test_leak_no_tags_or_branches_from_upstream(workspace: Workspace) -> None:
    """上游的 tag 和分支都没带过来。`v0.2.0` 这种 tag 本身就是"修复在这之前"的提示。"""
    assert git(workspace.path, "tag", "--list") == ""
    refs = git(workspace.path, "for-each-ref", "--format=%(refname)").splitlines()
    assert refs == ["refs/heads/main"]


def test_leak_workspace_content_is_the_buggy_version(workspace: Workspace) -> None:
    """工作区里是有 bug 的那一版代码，不是修好的那一版。"""
    source = (workspace.path / "src" / "app.py").read_text(encoding="utf-8")
    assert source == BUGGY_SOURCE
    assert source != FIXED_SOURCE


# ── 验收标准 3：两次物化结果一致 ────────────────────────────


def test_determinism_same_tree_hash(
    mirror_of: Path, source_repo: SourceRepo, tmp_path: Path
) -> None:
    """同一个 commit 物化两次，目录树哈希必须一样。"""
    first = materialize_workspace(
        mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=tmp_path / "a"
    )
    second = materialize_workspace(
        mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=tmp_path / "b"
    )
    assert first.tree_sha == second.tree_sha
    assert first.file_count == second.file_count


def test_determinism_same_commit_sha(
    mirror_of: Path, source_repo: SourceRepo, tmp_path: Path
) -> None:
    """连提交 SHA 都一样 —— 提交人和时间都是写死的常量。

    这一条不在验收标准里，但拿到了就该守住：commit SHA 相同意味着整个 `.git`
    的内容都可比对，将来排查"两次跑结果不同"时能直接锁定是不是工作区的问题。
    """
    first = materialize_workspace(
        mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=tmp_path / "a"
    )
    second = materialize_workspace(
        mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=tmp_path / "b"
    )
    assert first.base_sha == second.base_sha


def test_determinism_tree_equals_upstream_tree(
    workspace: Workspace, mirror_of: Path, source_repo: SourceRepo
) -> None:
    """工作区的树哈希等于上游那个 commit 的树哈希。

    这是"物化没丢东西也没多东西"的最强证据：git 的树哈希覆盖了每个文件的
    路径、权限位和内容，任何一处对不上，哈希就不同。
    """
    upstream_tree = run_git(
        ["rev-parse", f"{source_repo.base_commit}^{{tree}}"], cwd=mirror_of, timeout_s=60
    ).stdout.strip()
    assert workspace.tree_sha == upstream_tree


# ── 物化出来的内容对不对 ────────────────────────────────────


def test_executable_bit_survives(workspace: Workspace) -> None:
    """带可执行位的脚本物化之后还能执行。丢了权限位，测试命令可能直接起不来。"""
    assert is_executable(workspace.path / "scripts" / "run.sh")


def test_tracked_file_matching_ignore_baseline_is_still_committed(
    workspace: Workspace,
) -> None:
    """仓库跟踪的 `debug.log` 命中基线里的 `*.log`，但它必须照样进 base 提交。

    这一条盯的是 `git add --all --force` 里的 `--force`。少了它，
    工作区会比 base 少一个文件，而且不报错 —— 只是树哈希对不上。
    """
    assert "*.log" in DEFAULT_WORKSPACE_IGNORE
    assert (workspace.path / "debug.log").is_file()
    assert "debug.log" in git(workspace.path, "ls-files").splitlines()


def test_repo_own_gitignore_is_untouched(workspace: Workspace) -> None:
    """仓库自己的 `.gitignore` 原样保留 —— 基线清单写在 `.git/info/exclude` 里。"""
    assert (workspace.path / ".gitignore").read_text(encoding="utf-8") == "*.tmp\n"


def test_baseline_exclude_file_is_written(workspace: Workspace) -> None:
    exclude = (workspace.path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    for pattern in ("__pycache__/", ".aider*", "*.log"):
        assert pattern in exclude


def test_extra_ignore_is_appended(mirror_of: Path, source_repo: SourceRepo, tmp_path: Path) -> None:
    """环境规格可以追加规则，基线那份不受影响。"""
    ws = materialize_workspace(
        mirror_path=mirror_of,
        base_commit=source_repo.base_commit,
        dest=tmp_path / "ws",
        extra_ignore=("generated_fixtures/",),
    )
    exclude = (ws.path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "generated_fixtures/" in exclude
    assert "__pycache__/" in exclude


def test_workspace_starts_clean(workspace: Workspace) -> None:
    """刚物化出来的工作区是干净的 —— 之后 `git status` 里的任何东西都是 Agent 干的。"""
    assert git(workspace.path, "status", "--porcelain") == ""


def test_agent_can_commit_without_configuring_identity(workspace: Workspace) -> None:
    """Agent 在工作区里 `git commit` 不会撞上"Please tell me who you are"。

    有些 Agent 干完活习惯自己提交一次。身份没配好的话它会失败重试，
    白白烧掉几轮预算。身份写在工作区的 `.git/config` 里，容器里也带得过去。
    """
    write(workspace.path, "src/fix.py", "def patched():\n    return 1\n")
    run_git(["add", "--all", "--", "."], cwd=workspace.path, timeout_s=60)
    run_git(["commit", "--quiet", "--message", "agent fix"], cwd=workspace.path, timeout_s=60)
    assert len(git(workspace.path, "log", "--oneline").splitlines()) == 2


# ── 基线忽略清单挡对了东西 ──────────────────────────────────


def _drop_agent_noise(workspace: Workspace) -> None:
    """模拟 Agent 干完活之后工作区里的样子：一个真改动 + 一堆碎屑。"""
    write(workspace.path, "src/new_module.py", "def helper():\n    return 42\n")
    write(workspace.path, "src/__pycache__/app.cpython-311.pyc", "字节码")
    write(workspace.path, "tests/__pycache__/test_app.cpython-311.pyc", "字节码")
    write(workspace.path, ".aider.chat.history.md", "# 对话记录\n")
    write(workspace.path, "run.log", "跑测试的输出\n")
    write(workspace.path, ".pytest_cache/CACHEDIR.TAG", "Signature: 8a477f597d28d172\n")


def test_agent_noise_is_ignored(workspace: Workspace) -> None:
    """碎屑不进 `git status`，Agent 真写的源文件要进。"""
    _drop_agent_noise(workspace)
    assert workspace.untracked_files() == ["src/new_module.py"]


def test_untracked_with_ignored_sees_everything(workspace: Workspace) -> None:
    """`include_ignored=True` 时连被忽略的文件也列出来。

    C-63 删除"AI 新建的受保护文件"那一步必须用这个视角：
    `tests/__pycache__/*.pyc` 在受保护路径下，但它被基线挡住了，
    默认视角看不见也就删不掉。
    """
    _drop_agent_noise(workspace)
    everything = workspace.untracked_files(include_ignored=True)
    assert "tests/__pycache__/test_app.cpython-311.pyc" in everything
    assert "src/new_module.py" in everything
    assert ".aider.chat.history.md" in everything


# ── 坏输入要被拦住 ──────────────────────────────────────────


def test_rejects_non_empty_dest(mirror_of: Path, source_repo: SourceRepo, tmp_path: Path) -> None:
    """往非空目录物化会得到新旧混合的树，必须拒绝。"""
    dest = tmp_path / "ws"
    dest.mkdir()
    (dest / "leftover.txt").write_text("上一次跑剩下的\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="非空"):
        materialize_workspace(mirror_path=mirror_of, base_commit=source_repo.base_commit, dest=dest)


def test_rejects_short_sha(mirror_of: Path, source_repo: SourceRepo, tmp_path: Path) -> None:
    """短 SHA 在大仓库里会撞，禁止（§7.2(2)）。"""
    with pytest.raises(MirrorError, match="40 位"):
        materialize_workspace(
            mirror_path=mirror_of,
            base_commit=source_repo.base_commit[:8],
            dest=tmp_path / "ws",
        )


def test_rejects_branch_name(mirror_of: Path, tmp_path: Path) -> None:
    """分支名会移动，今天和半年后指向的树可能不同。"""
    with pytest.raises(MirrorError, match="40 位"):
        materialize_workspace(mirror_path=mirror_of, base_commit="main", dest=tmp_path / "ws")


def test_missing_commit_reports_the_sha(mirror_of: Path, tmp_path: Path) -> None:
    absent = "0" * 40
    with pytest.raises(WorkspaceError, match=absent):
        materialize_workspace(mirror_path=mirror_of, base_commit=absent, dest=tmp_path / "ws")


def test_export_ignore_is_caught(source_repo: SourceRepo, tmp_path: Path) -> None:
    """`.gitattributes` 里的 `export-ignore` 会让 `git archive` 悄悄少导出文件。

    这是物化最阴的一种失败：没有任何报错，工作区就是少了 `tests/`，
    然后测试阶段报"找不到用例"，排查方向全在测试执行器上，白费半天。
    自查必须在物化这一步就把它拦下来，并且把原因说出来。
    """
    write(source_repo.path, ".gitattributes", "tests/ export-ignore\n")
    trap_commit = commit_all(source_repo.path, "chore: 加 export-ignore")

    mirror = tmp_path / "trap.git"
    run_git(["clone", "--mirror", "--quiet", "--", str(source_repo.path), str(mirror)])

    with pytest.raises(WorkspaceError) as caught:
        materialize_workspace(mirror_path=mirror, base_commit=trap_commit, dest=tmp_path / "ws")
    message = str(caught.value)
    assert "tests/test_app.py" in message
    assert "export-ignore" in message


# ── 清理 ────────────────────────────────────────────────────


def test_remove_workspace_deletes_under_root(workspace: Workspace, tmp_path: Path) -> None:
    remove_workspace(workspace.path, root=tmp_path / "ws")
    assert not workspace.path.exists()


def test_remove_workspace_refuses_outside_root(workspace: Workspace, tmp_path: Path) -> None:
    """拼错路径时宁可报错，也不能顺手把别的目录删了。"""
    with pytest.raises(WorkspaceError, match="拒绝删除"):
        remove_workspace(workspace.path, root=tmp_path / "somewhere-else")
    assert workspace.path.exists()


def test_remove_workspace_refuses_root_itself(tmp_path: Path) -> None:
    """删根目录等于把所有工作区一起端了。"""
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(WorkspaceError, match="拒绝删除"):
        remove_workspace(root, root=root)
    assert root.exists()
