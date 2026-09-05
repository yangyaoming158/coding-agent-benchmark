"""Oracle 哨兵：交出官方补丁（E3-T2）。

它是**假阴性探针**。官方补丁是这道题公认的正确答案，所以在一个健康的题库上，
Oracle 的解决率必须是 **100%**（协议 C-50）。不到 100% 意味着下面三件事之一，
而且每一件都必须在发布数据集之前查清楚：

- 有坏题（`test_patch` 拆错了、F2P 用例 ID 写错了、镜像缺依赖）；
- 判定引擎有 bug（最常见的是用例 ID 归一化，见 AGENTS.md §5.5）；
- 补丁应用这一步有问题（行尾、编码、`git apply` 参数）。

所以 Oracle 是**数据集发布门槛**，不是一个可选的自测工具。

## 官方补丁从哪来

外部注入，不自己去读题库。两个理由：

1. `app.runner` 在分层里压在 `app.benchmark` 下面（`pyproject.toml` 的
   import-linter 契约），import 不到 `TaskDefinition`。
2. 编排层已经把题读出来了。再读一遍就有两份数据源，哪天它们不一致，
   "Oracle 解决率"到底是拿哪份补丁跑出来的说不清楚。

## 查不到补丁时不许静默交空手

查不到就报 `gold_patch_missing` 并把 `exit_code` 置成 1。悄悄交个空补丁的话，
这道题会被判成 UNRESOLVED，解决率从 100% 掉下来，而排查的人会去翻判定引擎——
真正的原因（补丁没喂进来）在那里根本看不出来。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.runner.adapters.base import (
    GOLD_PATCH_MISSING,
    PatchSource,
    as_lookup,
    sentinel_result,
)
from app.runner.protocol import (
    AgentConfig,
    AgentError,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
)


class OracleRunner:
    """交官方补丁的哨兵。

    构造时喂一份 `task_id → gold_patch`：

        runner = OracleRunner({task.task_id: task.gold_patch for task in tasks})

    题多的时候也可以喂一个按需查的函数，签名是 `(task_id) -> str | None`。
    """

    name = "oracle"

    def __init__(self, gold_patches: PatchSource | None = None) -> None:
        self._lookup = as_lookup(gold_patches)

    def probe(self) -> ProbeResult:
        """永远可用。补丁在不在是**每道题**的事，探活探不出来，别在这里假装能探。"""
        return ProbeResult(ok=True, agent_version="1.0", detail="Oracle 哨兵，无外部依赖")

    def run(self, task: AgentTaskInput, workspace: object, config: AgentConfig) -> AgentRunResult:
        """把这道题的官方补丁原样交出去。

        **不看 deadline**，这一条是刻意的。Oracle 的用途是证明"这套 harness 不会
        把正确答案判错"，它的输出必须只由 task_id 决定。要是让它在截止时刻已过时
        改交空手，那么编排层哪天算错了一次 deadline，Oracle 的解决率就会掉，
        而查的人会以为是判定引擎坏了。稳定压倒真实感。

        `workspace` 一眼都不看：官方补丁是完整的 diff，打补丁是判定链后面的事。
        """
        started_at = datetime.now(UTC)
        patch = self._lookup(task.task_id)
        if patch is None:
            return sentinel_result(
                agent_name=self.name,
                started_at=started_at,
                exit_code=1,
                error=AgentError(
                    code=GOLD_PATCH_MISSING,
                    message=f"没给 {task.task_id} 的官方补丁，Oracle 无从下手",
                ),
            )
        return sentinel_result(agent_name=self.name, started_at=started_at, patch=patch)


__all__ = ["OracleRunner"]
