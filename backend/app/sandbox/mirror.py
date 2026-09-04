"""Bare mirror 管理（§7.2(1) 第 1 步）。

评测**不在运行时 clone GitHub**。每个仓库在本地留一份 bare mirror
（`var/mirrors/{owner}__{repo}.git`），物化工作区时从这份镜像 `git archive`。

三个理由，按重要性排：

1. **可复现**：上游仓库可以 force-push、删分支、改 tag，甚至整个下架。镜像一旦拉下来，
   同一个 `base_commit` 明年还能导出同样的文件树。
2. **快**：一次评测跑几百个 task_run，每次都联网 clone 是几小时的纯等待。
3. **稳**：这台开发机出网要过代理，代理会抖（AGENTS.md 第 10 节）。
   镜像把网络依赖收敛到"第一次拉取"和"偶尔 fetch"两个时刻。

## 只有 `ensure_commit()` 会联网

调用方要的其实永远是同一句话："让本地有一份含这个 commit 的镜像"。
`ensure_commit()` 就是这句话：镜像不在就 clone，commit 不在就 fetch 一次再看，
还不在就报 `CommitNotFoundError`（说明 base_commit 写错了，或者上游把它 gc 掉了）。

其余方法都是纯本地的，不碰网络。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.sandbox.git_cli import DEFAULT_GIT_TIMEOUT_S, GitError, run_git

#: 仓库全名的合法形式：`owner/repo`。GitHub 的用户名和仓库名只允许字母、数字和 `.-_`。
#:
#: 卡这条不是洁癖：`repo_name` 来自 GitHub 挖掘（E1-T4），是**外部数据**，
#: 它会被拼进镜像目录名。不校验的话，一个 `../../etc` 就能让 clone 写到镜像根目录外面。
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 40 位小写全 SHA。
#:
#: 和 `app.benchmark.schema.FULL_SHA_PATTERN` 是同一条规则，这里重写一遍是因为
#: 模块边界不允许 sandbox 依赖 benchmark（benchmark 在更上层，见 pyproject 的
#: import-linter 契约）。一个"40 位十六进制"的正则不会变，重复的代价可以接受。
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class MirrorError(RuntimeError):
    """镜像相关的失败。映射到 `infra_outcome = WORKSPACE_ERROR`。"""


class CommitNotFoundError(MirrorError):
    """镜像里没有这个 commit，fetch 过一次之后仍然没有。

    两种真实原因：`base_commit` 填错了（比如填了短 SHA 或者别的仓库的 SHA），
    或者上游把那个提交删掉了（force-push 后被 gc）。两种都属于题目坏了，
    应该在题目验证阶段（§7.3 的 S2）就被拦下来。
    """


def validate_repo_name(repo_name: str) -> str:
    """检查 `owner/repo` 合法，返回它本身。"""
    if not REPO_NAME_PATTERN.match(repo_name):
        raise MirrorError(f"repo_name 必须是 owner/repo 形式：{repo_name!r}")
    if any(segment in (".", "..") for segment in repo_name.split("/")):
        raise MirrorError(f"repo_name 里不能有 . 或 .. 段：{repo_name!r}")
    return repo_name


def validate_commit(commit: str) -> str:
    """检查 40 位全 SHA，返回它本身。

    禁止短 SHA / 分支名 / tag（§7.2(2)）：分支和 tag 会移动，短 SHA 在大仓库里会撞。
    这三种写法都能让 `git archive` 成功，但导出的树在半年后可能就不是同一棵了。
    """
    if not FULL_SHA_PATTERN.match(commit):
        raise MirrorError(
            f"base_commit 必须是 40 位小写全 SHA，禁止短 SHA / 分支名 / tag：{commit!r}"
        )
    return commit


def mirror_dir_name(repo_name: str) -> str:
    """`owner/repo` → `owner__repo.git`。

    用 `__` 而不是保留目录层级：镜像根目录下就是一层平铺的 `*.git`，
    `ls var/mirrors` 一眼能看完，也不会有"删空目录"这类琐事。
    分隔符和题目 ID 的 `{owner}__{repo}-{pr}` 保持一致。
    """
    owner, repo = validate_repo_name(repo_name).split("/", 1)
    return f"{owner}__{repo}.git"


class MirrorManager:
    """管理 `root` 目录下的一批 bare mirror。

    用法：

        mirrors = MirrorManager(settings.mirror_root)
        path = mirrors.ensure_commit("psf/requests", "https://github.com/psf/requests.git", sha)
        # 之后把 path 交给 materialize_workspace()
    """

    def __init__(self, root: Path, *, timeout_s: int = DEFAULT_GIT_TIMEOUT_S) -> None:
        self.root = Path(root)
        self.timeout_s = timeout_s

    # ── 纯本地 ────────────────────────────────────────────────

    def path_for(self, repo_name: str) -> Path:
        """这个仓库的镜像该放在哪。不保证它已经存在。"""
        return self.root / mirror_dir_name(repo_name)

    def exists(self, repo_name: str) -> bool:
        """镜像在不在。认的是 `HEAD` 文件 —— 目录建了一半（clone 中途失败）不算数。"""
        return (self.path_for(repo_name) / "HEAD").is_file()

    def has_commit(self, repo_name: str, commit: str) -> bool:
        """镜像里有没有这个 commit 对象。

        用 `cat-file -e <sha>^{commit}`：只问"对象在不在、是不是提交"，不解析内容。
        末尾的 `^{commit}` 不能省 —— 少了它，一个恰好同名的 tree 或 blob 也会返回真。
        """
        validate_commit(commit)
        if not self.exists(repo_name):
            return False
        result = run_git(
            ["cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=self.path_for(repo_name),
            timeout_s=60,
            check=False,
        )
        return result.returncode == 0

    def tree_sha(self, repo_name: str, commit: str) -> str:
        """这个 commit 的顶层目录树哈希。

        它是"物化对不对"的标尺：工作区物化完之后自己也有一个树哈希，
        两者必须相等（见 `workspace.materialize_workspace`）。
        """
        validate_commit(commit)
        return run_git(
            ["rev-parse", "--verify", f"{commit}^{{tree}}"],
            cwd=self.path_for(repo_name),
            timeout_s=60,
        ).stdout.strip()

    # ── 会联网 ────────────────────────────────────────────────

    def clone(self, repo_name: str, repo_url: str) -> Path:
        """拉一份新镜像。已经存在就直接返回，不重复拉。"""
        path = self.path_for(repo_name)
        if self.exists(repo_name):
            return path
        _reject_option_like(repo_url, "repo_url")
        self.root.mkdir(parents=True, exist_ok=True)
        # `--` 把 URL 和选项隔开：repo_url 是外部数据，不加这一道，
        # 一个以 `-` 开头的 URL 就变成了 git 的选项（比如 --upload-pack=...）。
        run_git(
            ["clone", "--mirror", "--quiet", "--", repo_url, str(path)],
            timeout_s=self.timeout_s,
        )
        return path

    def fetch(self, repo_name: str) -> None:
        """从上游刷新所有引用。

        `--prune` 会删掉上游已经不存在的分支引用，但**不会删对象**：
        已经被某道题引用的 commit 即使上游删了分支，本地对象仍在，直到有人跑 gc。
        这正是我们要的 —— 题目一旦建好就不该因为上游改动而失效。
        """
        if not self.exists(repo_name):
            raise MirrorError(f"镜像不存在，先 clone：{self.path_for(repo_name)}")
        run_git(
            ["remote", "update", "--prune"],
            cwd=self.path_for(repo_name),
            timeout_s=self.timeout_s,
        )

    def ensure_commit(self, repo_name: str, repo_url: str, commit: str) -> Path:
        """保证本地有一份含这个 commit 的镜像，返回镜像路径。

        这是调用方唯一需要的方法。执行顺序：

            镜像不在 → clone
            commit 不在 → fetch 一次 → 还不在 → CommitNotFoundError

        **只 fetch 一次。** 再多试也没用：commit 不在上游的引用可达范围里，
        重复拉取只会浪费几分钟，而它多半是题目本身写错了。
        """
        validate_commit(commit)
        self.clone(repo_name, repo_url)
        if self.has_commit(repo_name, commit):
            return self.path_for(repo_name)

        try:
            self.fetch(repo_name)
        except GitError as exc:
            raise MirrorError(f"{repo_name} 的镜像里没有 {commit}，尝试 fetch 也失败了") from exc

        if not self.has_commit(repo_name, commit):
            raise CommitNotFoundError(
                f"{repo_name} 的镜像里没有 commit {commit}（fetch 之后仍然没有）。"
                f"常见原因：base_commit 填错，或者上游 force-push 之后这个提交被回收了"
            )
        return self.path_for(repo_name)


def _reject_option_like(value: str, field: str) -> None:
    """挡住以 `-` 开头的取值，避免它被 git 当成选项。"""
    if value.startswith("-"):
        raise MirrorError(f"{field} 不能以 - 开头，会被 git 当成选项：{value!r}")


__all__ = [
    "FULL_SHA_PATTERN",
    "REPO_NAME_PATTERN",
    "CommitNotFoundError",
    "MirrorError",
    "MirrorManager",
    "mirror_dir_name",
    "validate_commit",
    "validate_repo_name",
]
