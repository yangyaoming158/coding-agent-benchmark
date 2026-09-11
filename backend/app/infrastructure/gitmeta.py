"""harness 自己的 git 状态（协议 C-27、C-28）。

单独摆一个模块，是因为有两个互不可见的调用方都要用它：`cli/dataset.py` 发布数据集
时要记 `harness_git_sha`，`app/evaluation/manifest.py` 建实验时要拿同一份事实。
放在 `infrastructure` 层两边都能 import（模块依赖方向见 `AGENTS.md` 第 8 节）。

E5-T4 之前这段代码在 `cli/dataset.py` 里，注释写着"完整的强制由 E5-T4 做"。
现在搬到这里，两处读的是同一份实现 —— 两份实现迟早会在"什么叫干净"上分岔。
"""

from __future__ import annotations

import subprocess

from app.infrastructure.config import REPO_ROOT

#: 取不到 sha 时写进记录的占位值。不编一个假的 sha：**假的 sha 比没有更糟**，
#: 它会让"按这个版本重放"看起来可行，实际 checkout 不出任何东西。
UNKNOWN_SHA = "unknown"


def git_state() -> tuple[str, bool]:
    """返回 `(当前 commit sha, 工作区脏不脏)`。

    协议 C-27 要求正式实验启动前工作区必须干净：`harness_git_sha` 只有在没有未提交
    改动时才唯一代表一份代码，否则"可复现"是假的。C-28 留了 `--allow-dirty` 这个
    口子，但放行的结果必须标 `dirty = true` 且不得进排行榜。

    这里**只取事实，不做判断**。拒不拒绝由 `app.evaluation.manifest.collect_provenance()`
    决定 —— 它是唯一一处知道"这次是正式实验还是测试在建假数据"的地方。
    """

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    sha = run("rev-parse", "HEAD") or UNKNOWN_SHA
    return sha, bool(run("status", "--porcelain"))


__all__ = ["UNKNOWN_SHA", "git_state"]
