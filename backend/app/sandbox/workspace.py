"""工作区物化与防泄题（协议 C-43、§7.2(1)、§10.2）。

一句话：**把 base commit 的文件树导出来，在里面重新 `git init`，只提交一次。**

    git -C <mirror> archive <base_commit>  →  解包到工作区
    git init  →  写 .git/info/exclude  →  git add -A -f  →  git commit

结果是一个内容等于 base 树、但 git 历史里**只有一个提交**的工作区。

## 为什么不能 clone 再 checkout

被测 AI 在工作区里有完整的 shell。如果工作区是 clone 出来的，它一句

    git log origin/main

就能翻到 base 之后的所有提交 —— 包括修好这个 bug 的那个官方提交，连补丁内容
都是现成的。这不是理论风险，是一行命令的事。C-43 因此把"历史剥离"写成硬性规定。

`git archive` 导出的是**纯文件树**，一个 git 对象都不带。工作区里那唯一的提交是
我们自己在本地建的，它没有父提交，也没有 remote。

## 一次评测要物化两次

§10.2 的双容器设计里，工作区物化发生在两个阶段，用的是**同一个函数、不同的目录**：

    PREPARING  → materialize_workspace(dest=.../agent)  交给 Agent 容器改
    TESTING    → materialize_workspace(dest=.../test)   全新的，再打补丁再跑测试

测试阶段**绝不复用** Agent 用过的那份（C-15）。Agent 可能在里面 `pip install` 了东西、
改了配置、留了临时文件，拿它跑测试，测出来的就不只是"补丁对不对"了。

## 哪些改动会被忽略

工作区的 `.git/info/exclude` 里写了一份基线忽略清单（`DEFAULT_WORKSPACE_IGNORE`），
挡的是 Agent 干活时掉下来的碎屑：`__pycache__/`、`.pytest_cache/`、`.aider*` 之类。
不挡的话，这些会全部进入 `git diff`，污染补丁（ADR-007 的 Risk 一条）。

**为什么不是工作区根目录下的 `.gitignore`**：仓库自己可能就有一个 `.gitignore`，
写进去要么覆盖它、要么和它打架，而且那是一个**被跟踪文件的改动** ——
工作区的树哈希会因此和 base 树对不上，"两次物化一致"和"内容等于 base"都不成立了。
`.git/info/exclude` 是 git 专门给这种"仓库本地、不入库"的忽略规则准备的位置。

**基线只影响物化之后新出现的文件。** 建 base 提交时用的是 `git add -A --force`，
仓库里本来就跟踪的文件即使命中忽略规则也照样提交。否则一个跟踪着 `debug.log`
的仓库，物化出来会少一个文件。

## 留给 E4 的接口

强制还原受保护文件（C-16、C-63）是 E4 的事，不在这里。但两件事在这里定死了，
E4 直接用就行：

- 工作区是个正常的 git 仓库，`git checkout -- <path>` 能还原被跟踪的文件；
- `Workspace.untracked_files(include_ignored=True)` 列出新增文件。
  **找受保护的新增文件时必须带 `include_ignored=True`** —— 理由见那个方法的注释。
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.sandbox.git_cli import DEFAULT_GIT_TIMEOUT_S, GitError, hermetic_env, run_git
from app.sandbox.mirror import validate_commit

#: 工作区里那个唯一提交的固定身份与时间。
#:
#: 写死是为了可复现：用真实时间的话，同一个 base commit 每次物化都得到不同的 commit SHA，
#: "两次物化结果一致"就只能退而求其次比目录树。固定之后连 commit SHA 都是一样的。
#: 时间取 2000-01-01T00:00:00Z，没有特别含义，只要是个常量即可。
BASE_COMMIT_MESSAGE = "base"
BASE_COMMIT_AUTHOR_NAME = "bench"
BASE_COMMIT_AUTHOR_EMAIL = "bench@localhost"
BASE_COMMIT_DATE = "2000-01-01T00:00:00+00:00"

#: 工作区初始分支名。显式写出来，免得 git 按自己的默认值来（不同版本不一样，还会打 hint）。
BASE_BRANCH = "main"

#: 基线忽略清单，写进工作区的 `.git/info/exclude`。
#:
#: 挑选原则：**只挡确定是机器生成的东西**。宁可漏挡也不能错挡 —— 两种错误代价差很远：
#:
#: - 漏挡（噪声进了补丁）：补丁大一点，测试照跑，判定基本不受影响；
#: - 错挡（把 Agent 真写的源文件挡掉了）：它的修复被悄悄丢掉，判成"没修好"。
#:   这种错不会报错，只会让解决率莫名其妙偏低 —— 最难查的一类问题。
#:
#: 所以 `build/`、`dist/` 这类"通常是产物、但也可能是仓库里真实存在的源码目录"
#: 一律不进这份清单，交给 E3-T3 的补丁归一化去按大小和扩展名过滤。
DEFAULT_WORKSPACE_IGNORE: tuple[str, ...] = (
    # ── Python 字节码与工具缓存 ──
    "__pycache__/",
    "*.py[cod]",
    "*.so",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".hypothesis/",
    # ── 覆盖率与打包产物 ──
    ".coverage",
    ".coverage.*",
    "htmlcov/",
    "*.egg-info/",
    ".eggs/",
    # ── 虚拟环境与依赖目录（Agent 有时会自己建一个）──
    ".venv/",
    "venv/",
    "node_modules/",
    # ── 被测 Agent 自己的工作文件 ──
    # aider 会在仓库里写 .aider.chat.history.md、.aider.tags.cache 等一串文件；
    # 其余几个是各家 CLI 的本地状态目录。
    ".aider*",
    ".claude/",
    ".codex/",
    ".cursor/",
    ".qwen/",
    ".gemini/",
    # ── 打补丁和编辑器留下的残渣 ──
    "*.orig",
    "*.rej",
    "*.swp",
    "*~",
    ".DS_Store",
    # ── 日志 ──
    "*.log",
)


class WorkspaceError(RuntimeError):
    """工作区物化失败。映射到 `infra_outcome = WORKSPACE_ERROR`（平台责任，可重试）。"""


@dataclass(frozen=True, slots=True)
class Workspace:
    """一个已经物化好的工作区。"""

    #: 工作区根目录，里面就是仓库的文件树，外加一个 `.git`。
    path: Path
    #: 上游的 base commit（40 位 SHA）。它**不在**工作区的 git 历史里，只是溯源信息。
    base_commit: str
    #: 工作区里那个唯一提交的 SHA。
    #:
    #: E3-T3 抓 Agent 改动时**要拿它当基准**（`git diff <base_sha>`），不要用裸的
    #: `git diff` —— 有些 Agent 干完活会自己 `git commit`，那时裸 diff 是空的。
    base_sha: str
    #: 顶层目录树哈希。同一个 base commit 物化两次，这个值必须一样（E2-T1 验收标准）。
    tree_sha: str
    #: 物化出来的文件数（不含目录）。
    file_count: int

    def untracked_files(self, *, include_ignored: bool = False) -> list[str]:
        """列出 Agent 新增的、还没被 git 跟踪的文件（仓库相对路径，POSIX 分隔符）。

        `include_ignored=False` 时按 `.git/info/exclude` 和仓库自己的 `.gitignore`
        过滤，得到的是"值得进补丁的新文件"。

        **C-63 那一步（删掉 AI 新建的受保护文件）必须传 `include_ignored=True`。**
        道理是这样的：基线里有 `__pycache__/`，于是 AI 新建的
        `tests/__pycache__/conftest.cpython-311.pyc` 默认列不出来，也就删不掉。
        受保护路径的清理要看到工作区里**所有**新文件，不能被忽略规则挡住视线。
        """
        args = ["ls-files", "--others", "-z"]
        if not include_ignored:
            args.append("--exclude-standard")
        raw = run_git(args, cwd=self.path, timeout_s=120).stdout
        return sorted(item for item in raw.split("\0") if item)

    def commit_count(self) -> int:
        """所有引用能到达的提交总数。物化完必须是 1（防泄题的可验证形式）。"""
        out = run_git(["rev-list", "--all", "--count"], cwd=self.path, timeout_s=60).stdout
        return int(out.strip())


def materialize_workspace(
    *,
    mirror_path: Path,
    base_commit: str,
    dest: Path,
    extra_ignore: Sequence[str] = (),
    timeout_s: int = DEFAULT_GIT_TIMEOUT_S,
) -> Workspace:
    """把 `base_commit` 的文件树物化成一个只有一个提交的工作区。

    `dest` 必须不存在或者是个空目录 —— 往非空目录里物化，得到的树是"旧内容 + 新内容"
    的混合，而且不会报错。这种工作区跑出来的判定结果是错的，且极难追查。

    `extra_ignore` 追加到基线忽略清单后面，给个别仓库开小灶用（比如某个仓库的测试
    会在仓库里生成固定名字的产物文件）。

    物化完会自查两条不变量，任何一条不成立都抛 `WorkspaceError`：

    1. 工作区的树哈希 == 镜像里那个 commit 的树哈希（内容一模一样）；
    2. 工作区的提交数 == 1（历史确实剥干净了）。
    """
    validate_commit(base_commit)
    mirror_path = Path(mirror_path)
    dest = Path(dest)

    source_tree = _source_tree_sha(mirror_path, base_commit)
    _prepare_dest(dest)

    try:
        file_count = _extract_archive(mirror_path, base_commit, dest, timeout_s=timeout_s)
        _init_repo(dest, extra_ignore=extra_ignore)
        base_sha = _commit_base_tree(dest, timeout_s=timeout_s)
    except GitError as exc:
        raise WorkspaceError(f"物化 {base_commit[:12]} 到 {dest} 失败：{exc}") from exc

    workspace = Workspace(
        path=dest,
        base_commit=base_commit,
        base_sha=base_sha,
        tree_sha=_workspace_tree_sha(dest),
        file_count=file_count,
    )
    _verify(workspace, mirror_path=mirror_path, source_tree=source_tree)
    return workspace


def remove_workspace(path: Path, *, root: Path) -> None:
    """删掉一个工作区。`path` 必须在 `root` 底下，否则拒绝。

    这道检查不是形式主义：工作区路径是由 run_id / task_run_id 拼出来的，
    拼错一次（比如某个 id 是 None，拼出 `var/workspaces/`）就是一条
    `rmtree` 把所有工作区一起端掉。限定在 root 之内，最坏也只损失工作区目录。
    """
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or root not in path.parents:
        raise WorkspaceError(f"拒绝删除 {path}：它不在工作区根目录 {root} 底下")
    shutil.rmtree(path, ignore_errors=True)


# ── 内部实现 ────────────────────────────────────────────────


def _source_tree_sha(mirror_path: Path, commit: str) -> str:
    """镜像里那个 commit 的顶层树哈希。顺便证明这个 commit 确实存在。"""
    try:
        return run_git(
            ["rev-parse", "--verify", f"{commit}^{{tree}}"], cwd=mirror_path, timeout_s=60
        ).stdout.strip()
    except GitError as exc:
        raise WorkspaceError(f"镜像 {mirror_path} 里找不到 commit {commit}：{exc}") from exc


def _prepare_dest(dest: Path) -> None:
    """确认目标目录可用，并建出来。"""
    if dest.exists():
        if not dest.is_dir():
            raise WorkspaceError(f"工作区路径已被一个文件占着：{dest}")
        if any(dest.iterdir()):
            raise WorkspaceError(
                f"工作区目录非空，拒绝物化：{dest}。"
                f"物化到非空目录会得到'旧内容 + 新内容'的混合树，而且不会报错"
            )
    dest.mkdir(parents=True, exist_ok=True)


def _extract_archive(mirror_path: Path, commit: str, dest: Path, *, timeout_s: int) -> int:
    """`git archive` 的输出直接流式解包到 `dest`，返回落盘的文件数。

    流式（`tar -x` 的等价物）而不是先落一个 tar 文件：大仓库的归档有几百 MB，
    多一次落盘就是多一次磁盘往返，而这条路径每个 task_run 要走两次。

    用 Python 的 `tarfile` 而不是外部 `tar` 命令，图的是 `filter="data"`：
    它会挡掉绝对路径、`../` 穿越、设备文件、setuid 位这些东西。`git archive`
    正常情况下不会产生它们，但解包是把外部数据写进文件系统的一步，该设的防线要设。

    `timeout_s` 管的是"git 写完了但进程不退出"这一种卡死。管不了"写到一半不动了"——
    那会阻塞在读管道上。这里可以接受：`git archive` 读的是本地 bare 仓库，
    不碰网络，真正需要超时保护的是会联网的 clone / fetch，那两条走 `run_git`。
    """
    command = ["git", "-C", str(mirror_path), "archive", "--format=tar", commit]
    file_count = 0
    # Popen 而不是 run：要边出边解，不把整个 tar 读进内存
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=hermetic_env()
    ) as proc:
        stream = proc.stdout
        if stream is None:  # pragma: no cover - stdout=PIPE 时不会发生，纯为类型收窄
            raise WorkspaceError("拿不到 git archive 的输出管道")
        try:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    archive.extract(member, dest, filter="data")
                    if member.isfile():
                        file_count += 1
        except (tarfile.TarError, OSError) as exc:
            proc.kill()
            raise WorkspaceError(f"解包 {commit[:12]} 的归档失败：{exc}") from exc
        finally:
            stream.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise WorkspaceError(f"git archive {commit[:12]} 超过 {timeout_s} 秒没退出") from exc

    if proc.returncode != 0:
        raise WorkspaceError(
            f"git archive {commit[:12]} 失败（退出码 {proc.returncode}）：{stderr}"
        )
    return file_count


def _init_repo(dest: Path, *, extra_ignore: Sequence[str]) -> None:
    """在工作区里建一个全新的空仓库，并写好基线忽略清单。

    `git init` 之后没有任何 remote、没有任何对象 —— 防泄题就落在这一步：
    工作区的 git 数据库里根本没有 base 之后的提交，`git log --all` 也就无从看起。
    """
    run_git(["init", "--quiet", f"--initial-branch={BASE_BRANCH}"], cwd=dest, timeout_s=60)
    # 写进仓库本地配置，不依赖运行环境。Agent 容器里的 git 读不到宿主机的全局配置，
    # 没有这两行的话，Agent 想 `git commit` 会撞上"Please tell me who you are"，
    # 白白浪费它的轮次。
    run_git(["config", "user.name", BASE_COMMIT_AUTHOR_NAME], cwd=dest, timeout_s=60)
    run_git(["config", "user.email", BASE_COMMIT_AUTHOR_EMAIL], cwd=dest, timeout_s=60)
    write_baseline_exclude(dest, extra_ignore=extra_ignore)


def write_baseline_exclude(dest: Path, *, extra_ignore: Sequence[str] = ()) -> Path:
    """把基线忽略清单写进 `<dest>/.git/info/exclude`，返回该文件路径。"""
    exclude_path = dest / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 评测平台的基线忽略清单（E2-T1）。",
        "# 挡的是被测 Agent 干活时掉下来的碎屑，避免它们进入 git diff 污染补丁。",
        "# 只对物化之后新出现的文件生效：base 提交是 `git add -A --force` 建的。",
        "",
        *DEFAULT_WORKSPACE_IGNORE,
    ]
    if extra_ignore:
        lines += ["", "# 环境规格追加的规则", *extra_ignore]
    exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return exclude_path


def _commit_base_tree(dest: Path, *, timeout_s: int) -> str:
    """把整棵树提交成唯一的一个提交，返回它的 SHA。

    `--force` 不能少：基线忽略清单里有 `*.log` 这类规则，而仓库里可能真的跟踪着
    一个 `debug.log`。不加 `--force`，那个文件会被漏掉 —— 工作区少一个文件，
    树哈希和 base 对不上，物化自查会直接失败。
    """
    run_git(["add", "--all", "--force", "--", "."], cwd=dest, timeout_s=timeout_s)
    # 作者和提交者时间都固定，commit SHA 才是确定的（两者都参与哈希）
    run_git(
        [
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "--no-verify",
            "--message",
            BASE_COMMIT_MESSAGE,
        ],
        cwd=dest,
        timeout_s=timeout_s,
        env_extra={
            "GIT_AUTHOR_NAME": BASE_COMMIT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": BASE_COMMIT_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": BASE_COMMIT_DATE,
            "GIT_COMMITTER_NAME": BASE_COMMIT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": BASE_COMMIT_AUTHOR_EMAIL,
            "GIT_COMMITTER_DATE": BASE_COMMIT_DATE,
        },
    )
    return run_git(["rev-parse", "HEAD"], cwd=dest, timeout_s=60).stdout.strip()


def _workspace_tree_sha(dest: Path) -> str:
    return run_git(["rev-parse", "HEAD^{tree}"], cwd=dest, timeout_s=60).stdout.strip()


def _verify(workspace: Workspace, *, mirror_path: Path, source_tree: str) -> None:
    """物化自查。两条不变量都不成立就抛错，绝不带着一个可疑的工作区往下走。"""
    if workspace.tree_sha != source_tree:
        raise WorkspaceError(_describe_tree_mismatch(workspace, mirror_path, source_tree))
    count = workspace.commit_count()
    if count != 1:
        raise WorkspaceError(
            f"工作区里有 {count} 个提交，应该只有 1 个（协议 C-43）：{workspace.path}"
        )


def _describe_tree_mismatch(workspace: Workspace, mirror_path: Path, source_tree: str) -> str:
    """树哈希对不上时，把差异逐条列出来，别只丢两个哈希值让人自己去猜。

    最常见的原因是仓库里有 `.gitattributes` 写了 `export-ignore`：那会让
    `git archive` **悄悄少导出**一批文件（`tests/` 被标记的情况真实存在），
    工作区因此缺文件，而整个过程一句报错都没有。这种题必须在验证阶段就拦下来。
    """
    expected = _list_tree(mirror_path, source_tree)
    actual = _list_tree(workspace.path, workspace.tree_sha)

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(p for p in set(expected) & set(actual) if expected[p] != actual[p])

    lines = [
        f"工作区内容和 base 树对不上：{workspace.path}",
        f"  期望树 {source_tree}，实际树 {workspace.tree_sha}",
    ]
    for label, paths in (("缺少", missing), ("多出", extra), ("内容或权限不同", changed)):
        if paths:
            shown = ", ".join(paths[:10])
            suffix = f" …共 {len(paths)} 个" if len(paths) > 10 else ""
            lines.append(f"  {label}：{shown}{suffix}")
    if missing:
        lines.append(
            "  最常见的原因：仓库的 .gitattributes 里有 export-ignore，"
            "git archive 会跳过这些路径。这道题应在题目验证阶段判为无效"
        )
    return "\n".join(lines)


def _list_tree(repo_path: Path, tree_sha: str) -> dict[str, str]:
    """把一棵树摊平成 `{路径: "模式 对象SHA"}`。

    子模块（gitlink，模式 160000）也在里面 —— 工作区那边肯定没有它们，
    于是会出现在"缺少"里，正好把"这道题带子模块"这件事说清楚。
    """
    out = run_git(
        ["ls-tree", "-r", "-z", "--full-tree", tree_sha], cwd=repo_path, timeout_s=120
    ).stdout
    entries: dict[str, str] = {}
    for record in out.split("\0"):
        if not record:
            continue
        # 格式：`<mode> <type> <object>\t<path>`
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) >= 3:
            entries[path] = f"{fields[0]} {fields[2]}"
    return entries


__all__ = [
    "BASE_BRANCH",
    "BASE_COMMIT_AUTHOR_EMAIL",
    "BASE_COMMIT_AUTHOR_NAME",
    "BASE_COMMIT_DATE",
    "BASE_COMMIT_MESSAGE",
    "DEFAULT_WORKSPACE_IGNORE",
    "Workspace",
    "WorkspaceError",
    "materialize_workspace",
    "remove_workspace",
    "write_baseline_exclude",
]
