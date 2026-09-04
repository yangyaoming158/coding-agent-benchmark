"""造一个"长得像真仓库"的测试仓库。

这些测试不联网、不用 Docker：`git clone --mirror` 对本地路径同样有效，
所以整套 E2-T1 的验收都能在几百毫秒内跑完，进每次提交的快速测试集。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.sandbox.git_cli import run_git

#: 建测试仓库时用的固定身份和时间。固定下来是为了让"同一份 fixture 两次跑出同样的
#: commit SHA"成立 —— 有几条用例要比对哈希。
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
}

#: 官方修复提交的信息。防泄题的用例要确认工作区里搜不到这句话。
FIX_COMMIT_MESSAGE = "fix: 登录时空密码不应通过校验"

BUGGY_SOURCE = """def login(user, password):
    # bug: 空密码也放行
    return True
"""

FIXED_SOURCE = """def login(user, password):
    return bool(password) and password == user.password
"""


@dataclass(frozen=True)
class SourceRepo:
    """一个带历史的普通仓库：base 提交之后还有官方修复和一个后续提交。"""

    path: Path
    #: 题目的 base commit —— bug 还在的那个状态。
    base_commit: str
    #: 官方修复提交。工作区里**绝不能**出现它。
    fix_commit: str
    #: 修复之后的又一个提交，用来确认整条后续历史都不可见。
    later_commit: str


def git(repo: Path, *args: str) -> str:
    """在 repo 里跑一条 git 命令，返回 stdout。"""
    return run_git(list(args), cwd=repo, timeout_s=60, env_extra=_COMMIT_ENV).stdout


def write(repo: Path, relative: str, content: str, *, executable: bool = False) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    if executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "--all", "--force", "--", ".")
    git(repo, "commit", "--quiet", "--no-verify", "--message", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> SourceRepo:
    """建一个上游仓库，历史是 base → 官方修复 → 后续提交。

    刻意放进去几样容易出事的东西：

    - `scripts/run.sh` 带可执行位 —— 物化时权限位不能丢，丢了脚本跑不起来；
    - `debug.log` 被仓库跟踪，而它命中基线忽略清单里的 `*.log` ——
      用来验证 base 提交是 `--force` 加的，跟踪文件不会被忽略规则吃掉；
    - 仓库自带 `.gitignore` —— 用来验证我们没去动它。
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")

    write(repo, "README.md", "# 示例项目\n\n一个用来测物化流程的小仓库。\n")
    write(repo, ".gitignore", "*.tmp\n")
    write(repo, "src/app.py", BUGGY_SOURCE)
    write(repo, "src/__init__.py", "")
    write(repo, "tests/test_app.py", "def test_login():\n    assert True\n")
    write(repo, "scripts/run.sh", "#!/bin/sh\npython -m src.app\n", executable=True)
    write(repo, "debug.log", "第一次运行的日志\n")
    base_commit = commit_all(repo, "feat: 初版登录")

    write(repo, "src/app.py", FIXED_SOURCE)
    fix_commit = commit_all(repo, FIX_COMMIT_MESSAGE)

    write(repo, "CHANGELOG.md", "## 0.2.0\n- 修了登录校验\n")
    later_commit = commit_all(repo, "docs: 补 CHANGELOG")

    git(repo, "tag", "v0.2.0")
    return SourceRepo(
        path=repo, base_commit=base_commit, fix_commit=fix_commit, later_commit=later_commit
    )


@pytest.fixture
def mirror_of(source_repo: SourceRepo, tmp_path: Path) -> Path:
    """`source_repo` 的 bare mirror，直接给物化用。"""
    mirror = tmp_path / "mirrors" / "example__demo.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--mirror", "--quiet", "--", str(source_repo.path), str(mirror)])
    return mirror


def is_executable(path: Path) -> bool:
    return os.access(path, os.X_OK)
