"""补丁捕获与归一化（E3-T3，`06-judge-attribution.md` §11.4）。

一句话：**把 Agent 在工作区里干的活抓成一段 diff，再把不该算数的部分整段丢掉。**

    git add -A → git diff <base_sha>   ──▶  原始补丁（AGENT_RAW，原样存档）
                                            │
                                            ├─ 丢掉受保护路径（C-41、C-62）
                                            ├─ 丢掉二进制、超大文件
                                            ├─ 丢掉噪声文件（__pycache__、.aider*）
                                            ├─ 丢掉只改权限和空段
                                            └─▶ 标准化补丁（AGENT_NORMALIZED，拿去判定）

两份都要留。只留后者的话，"AI 试图改测试文件"这个行为就再也查不到了，
而它是防作弊分析的主要证据（协议 C-08b）。

## 为什么按文件段整段取舍，不按行

删掉 hunk 里的几行之后，hunk 头 `@@ -1,7 +1,9 @@` 声明的行数就和实际行数对不上，
`git apply` 会报 `corrupt patch`。一个打不上的"标准化补丁"会被判成 `INVALID_PATCH`,
而责任其实在平台自己 —— 这类 bug 最难查，因为它看起来像 AI 交了个坏补丁。

所以取舍的最小单位是 `DiffSection`（`app.domain.patch_paths`）：一段要么整个留下、
要么整个丢掉，留下的部分逐字节不动。

## 三道过滤各自在防什么

| 丢弃原因 | 防的是 |
|:---|:---|
| `protected_path` | AI 改测试文件让它自己通过（C-41）。改名绕过也算，见 C-62 |
| `binary` / `oversized` | 一个几 MB 的二进制补丁会把制品库和判定链拖垮，而它对修 bug 没有帮助 |
| `noise` | `__pycache__/`、`.aider*` 这些是 Agent 干活掉下来的碎屑，进了补丁会污染统计 |
| `mode_only` / `empty_section` | 只改权限、或者干脆什么都没有的段。留着只会让补丁哈希不稳定 |

噪声其实有两道防线：工作区的 `.git/info/exclude`（E2-T1）让 `git add -A` 根本不会
把它们暂存进去；这里再拦一次，是因为**走 strict 模式的适配器直接交 diff 字符串**，
那条路不经过 git，`.git/info/exclude` 管不到。

## 受保护清单必须由调用方给

`normalize_patch()` 的 `protected_patterns` 是**必填**的，没有默认值。

理由：完整清单要含该题的 `test_patch_paths`（C-42 最后一条、C-74）—— 有些题的测试
改动会带上名字完全不像测试的 fixture 文件，靠通配符匹配不到。给一个"通用规则"的
默认值，等于让"忘了传该题的清单"变成一个不报错的选项，而后果是那几个文件不受保护、
AI 改了也生效，**解决率会静悄悄地偏高**。用 `app.domain.protected_paths.enforcement_patterns()`
生成，别用 `agent_visible_patterns()`（那份是下发给 AI 的，故意不含题目信息）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path

from app.domain.patch_paths import DiffSection, iter_diff_sections
from app.domain.protected_paths import is_protected, normalize_path
from app.sandbox.git_cli import DEFAULT_GIT_TIMEOUT_S, GitError, run_git
from app.sandbox.workspace import DEFAULT_WORKSPACE_IGNORE, Workspace

#: 单个文件的补丁超过这个大小就整段丢掉（§11.4 第 2 条）。
#:
#: 256 KB 是个经验值：真实的 bug 修复几乎都在几 KB 以内，上万行的单文件 diff
#: 基本只有三个来源 —— 生成的代码、格式化整个文件、误提交的数据文件。
#: 三种都不是我们要评的东西。
MAX_FILE_BYTES = 256 * 1024


class PatchCaptureError(RuntimeError):
    """从工作区抓不出补丁。映射到 `infra_outcome = HARNESS_ERROR`（平台责任）。"""


class FilterReason(StrEnum):
    """一段改动被丢掉的原因（协议 C-08b 的 `filtered_change_reasons`）。

    前四个是协议点名的，`noise` 是 E3-T3 任务卡追加的（`__pycache__`/`.aider*`）。
    取值用小写：它们要序列化进 `evaluation_task_runs.filtered_change_reasons` 那个
    JSONB 列，和列里其他键的风格保持一致。
    """

    PROTECTED_PATH = "protected_path"
    BINARY = "binary"
    OVERSIZED = "oversized"
    MODE_ONLY = "mode_only"
    NOISE = "noise"
    #: 既没有 hunk 也没有文件操作 —— 一段什么都没干的改动。
    EMPTY_SECTION = "empty_section"


@dataclass(frozen=True, slots=True)
class FilteredChange:
    """一条"这个文件的改动被丢了，因为……"的记录。"""

    path: str
    reason: FilterReason
    detail: str = ""

    def to_record(self) -> dict[str, str]:
        """转成能直接写进 JSONB 的形状。"""
        return {"path": self.path, "reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PatchStats:
    """一段补丁的统计。字段和 `patch_artifacts` 表的列一一对应。"""

    files_changed: int
    lines_added: int
    lines_deleted: int
    size_bytes: int
    sha256: str

    @property
    def is_empty(self) -> bool:
        """一个文件都没改。

        用 `files_changed` 而不是 `size_bytes` 判空：一段只有 `diff --git` 头、
        没有任何 hunk 的补丁，字节数不为 0，但它什么都没改。
        """
        return self.files_changed == 0


def patch_stats(patch: str) -> PatchStats:
    """算一段补丁的统计。空字符串给一份全零的统计，不报错。"""
    files = 0
    added = 0
    deleted = 0
    for section in iter_diff_sections(patch):
        files += 1
        added += section.lines_added
        deleted += section.lines_deleted
    data = patch.encode("utf-8")
    return PatchStats(
        files_changed=files,
        lines_added=added,
        lines_deleted=deleted,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class NormalizedPatch:
    """归一化的结果：拿去判定的补丁，加上判定和归因都要用的那几个数。"""

    #: 标准化之后的补丁。能用 `git apply --3way` 打上。
    text: str
    stats: PatchStats
    #: 原始补丁的统计。两份都要存 —— 只存标准化的，"AI 试图改测试文件"就没证据了。
    raw_stats: PatchStats
    #: 被丢掉的改动，按路径排序。
    filtered: tuple[FilteredChange, ...]

    @property
    def is_empty(self) -> bool:
        """标准化之后补丁为空。

        这就是协议 C-08a 说的 `EMPTY_PATCH` 的判据，**不等于**"AI 什么都没做" ——
        AI 改了一堆受保护文件想蒙混过关，全部被丢弃之后也是空的。
        两者靠 `raw_patch_empty` 和 `protected_path_edit_attempted` 区分。
        """
        return self.stats.is_empty

    @property
    def raw_patch_empty(self) -> bool:
        """过滤之前 AI 就什么都没改（协议 C-08b）。"""
        return self.raw_stats.is_empty

    @property
    def protected_path_edit_attempted(self) -> bool:
        """AI 试图改受保护路径下的文件（协议 C-08b）。

        为 true 时**本身就要触发人工复核**，即使最终判定正常（C-13d）。
        """
        return any(item.reason is FilterReason.PROTECTED_PATH for item in self.filtered)

    def filtered_change_reasons(self) -> list[dict[str, str]]:
        """写进 `evaluation_task_runs.filtered_change_reasons` 的那个 JSON 数组。"""
        return [item.to_record() for item in self.filtered]


def is_noise_path(path: str, patterns: Sequence[str] = DEFAULT_WORKSPACE_IGNORE) -> bool:
    """这个路径是不是 Agent 干活掉下来的碎屑。

    规则直接复用工作区的基线忽略清单（`app.sandbox.workspace.DEFAULT_WORKSPACE_IGNORE`），
    不另写一份 —— 两份清单迟早会不一致，而不一致时"到底什么算噪声"就没人说得清了。

    这里只实现 gitignore 语法里那两种形状，够覆盖整份清单：

    - `foo/` 结尾带斜杠：只匹配目录，也就是路径里除最后一段之外的任意一段；
    - `*.pyc`、`.aider*`、`.coverage`：匹配任意一段（含最后一段）。
      不带斜杠的规则命中目录时，git 会连目录下所有文件一起忽略，所以这里也按
      "任意一段命中就算"来判 —— aider 的 `.aider.tags.cache.v3/` 就是这种情况。

    **故意不做完整的 gitignore 引擎**（否定规则 `!`、锚定 `/foo`、`**` 跨级）：
    那是 git 自己的活，而这份清单里一条都没用到。真需要时应该去调 git，不是在这里
    重写一个语义不完全一样的实现。
    """
    segments = normalize_path(path).split("/")
    if not segments:
        return False
    for pattern in patterns:
        directory_only = pattern.endswith("/")
        core = pattern.rstrip("/")
        candidates = segments[:-1] if directory_only else segments
        if any(fnmatch(segment, core) for segment in candidates):
            return True
    return False


def _classify(
    section: DiffSection,
    *,
    protected_patterns: tuple[str, ...],
    noise_patterns: Sequence[str],
    max_file_bytes: int,
) -> FilteredChange | None:
    """这一段该不该丢。该丢就给出原因，该留返回 None。

    顺序有讲究：**受保护路径排在最前面**。一个 `tests/` 下的二进制大文件，
    报成 `binary` 就把"AI 动了测试目录"这条证据掩盖掉了，而那是要触发人工复核的
    信号（C-13d）。其余几条之间没有语义冲突，按从确定到模糊排。
    """
    label = section.paths[0] if section.paths else "（解析不出路径）"

    hits = [path for path in section.paths if is_protected(path, protected_patterns)]
    if hits:
        # C-62：改名和复制时旧新路径有一个中招，整段丢掉。`section.paths` 本来
        # 就把两个路径都收进来了，所以这里不需要为改名单独写一条分支。
        return FilteredChange(label, FilterReason.PROTECTED_PATH, f"命中受保护路径 {hits}")

    # 噪声只判**新建**的文件。仓库本来就跟踪着的文件即使名字像产物（真有仓库
    # 跟踪 `debug.log`、`*.so`），改它也是合法修复 —— 当噪声丢掉就是把 AI 的
    # 修复悄悄删了，最后判成"没修好"，而且一句报错都没有。
    # 这和工作区那边的规则是同一套语义：`.git/info/exclude` 只管物化之后新出现的
    # 文件，base 提交用的是 `git add -A --force`（见 `app.sandbox.workspace`）。
    noisy = [path for path in section.paths if is_noise_path(path, noise_patterns)]
    if noisy and section.is_new_file:
        return FilteredChange(label, FilterReason.NOISE, f"新建的机器生成文件 {noisy}")

    if section.is_binary:
        return FilteredChange(label, FilterReason.BINARY, "二进制文件")

    size = len(section.text.encode("utf-8"))
    if size > max_file_bytes:
        return FilteredChange(
            label, FilterReason.OVERSIZED, f"单文件补丁 {size} 字节，超过 {max_file_bytes}"
        )

    if section.is_mode_change_only:
        return FilteredChange(label, FilterReason.MODE_ONLY, "只改了文件权限，内容没动")

    if not section.has_content:
        return FilteredChange(label, FilterReason.EMPTY_SECTION, "这一段没有任何可应用的改动")

    return None


def normalize_patch(
    raw: str,
    *,
    protected_patterns: tuple[str, ...],
    max_file_bytes: int = MAX_FILE_BYTES,
    noise_patterns: Sequence[str] = DEFAULT_WORKSPACE_IGNORE,
) -> NormalizedPatch:
    """按 §11.4 把原始补丁过一遍。

    `protected_patterns` 没有默认值，理由见模块文档 —— 一句话：忘了传该题的
    `test_patch_paths` 会让解决率静悄悄偏高。

    留下来的段**逐字节不动**（除了结构行的行尾统一成 LF，见 `DiffSection`），
    所以输出必然还是一个合法补丁。
    """
    kept: list[str] = []
    filtered: list[FilteredChange] = []

    for section in iter_diff_sections(raw):
        verdict = _classify(
            section,
            protected_patterns=protected_patterns,
            noise_patterns=noise_patterns,
            max_file_bytes=max_file_bytes,
        )
        if verdict is None:
            kept.append(section.text)
        else:
            filtered.append(verdict)

    text = "".join(kept)
    return NormalizedPatch(
        text=text,
        stats=patch_stats(text),
        raw_stats=patch_stats(raw),
        filtered=tuple(sorted(filtered, key=lambda item: (item.path, item.reason.value))),
    )


def capture_workspace_diff(workspace: Workspace, *, timeout_s: int = DEFAULT_GIT_TIMEOUT_S) -> str:
    """把 Agent 在工作区里干的活抓成一段 diff。

    两步：

        git add --all       把新文件也纳进来，同时尊重 .git/info/exclude
        git diff --cached <base_sha>

    **为什么先 `git add`**：`git diff` 只看被跟踪的文件，Agent 新建的源文件
    根本不在里面。修 bug 时新建一个模块是完全正常的行为，漏掉它等于把修复删了。
    暂存这一步会写工作区的 index —— 没关系，这份工作区抓完补丁就不再用了，
    跑测试用的是另外新物化的一份（协议 C-15）。

    **为什么基准是 `base_sha` 而不是裸 `git diff`**：有些 Agent 干完活会自己
    `git commit`，那时裸 diff 是空的，看起来像"它什么都没做"。

    `--find-renames` 显式打开改名识别：C-62 要求旧新路径都参与受保护判断，
    识别不出改名就只剩一个路径，绕过去了。
    """
    try:
        run_git(["add", "--all", "--", "."], cwd=workspace.path, timeout_s=timeout_s)
        result = run_git(
            [
                "diff",
                "--cached",
                "--no-color",
                "--no-ext-diff",
                "--find-renames",
                workspace.base_sha,
            ],
            cwd=workspace.path,
            timeout_s=timeout_s,
        )
    except GitError as exc:
        raise PatchCaptureError(f"从 {workspace.path} 抓补丁失败：{exc}") from exc
    return result.stdout


def capture_agent_patch(
    workspace: Workspace,
    *,
    protected_patterns: tuple[str, ...],
    max_file_bytes: int = MAX_FILE_BYTES,
    timeout_s: int = DEFAULT_GIT_TIMEOUT_S,
) -> NormalizedPatch:
    """抓 + 归一化一步到位。走 workspace-mutation 型适配器的那条路用它。

    strict 模式的适配器（AI 自己打印 diff）不经过工作区，直接拿它交上来的字符串
    调 `normalize_patch()`。两条路最后得到的都是 `NormalizedPatch`。
    """
    return normalize_patch(
        capture_workspace_diff(workspace, timeout_s=timeout_s),
        protected_patterns=protected_patterns,
        max_file_bytes=max_file_bytes,
    )


def write_patch(patch: str, dest: Path) -> Path:
    """把补丁写成文件，给 `git apply` 用。始终以换行结尾。

    补丁最后一行没有换行时，`git apply` 会报 `corrupt patch at line N` —— 这条
    在手工拼补丁的路径上真的会踩到，所以统一在这里补上。
    """
    text = patch if patch.endswith("\n") or not patch else patch + "\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


__all__ = [
    "MAX_FILE_BYTES",
    "FilterReason",
    "FilteredChange",
    "NormalizedPatch",
    "PatchCaptureError",
    "PatchStats",
    "capture_agent_patch",
    "capture_workspace_diff",
    "is_noise_path",
    "normalize_patch",
    "patch_stats",
    "write_patch",
]
