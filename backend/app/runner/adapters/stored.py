"""重放上一次已经拿到的补丁（协议 C-54）。

## 它解决的问题

协议 C-54 写死了：**已经拿到补丁之后才发生的测试阶段故障，重试时必须复用同一份
标准化补丁，禁止重新调用被测 AI。**

典型场景是 `TEST_TIMEOUT`、`OOM_KILLED`、`TEST_DISCOVERY_ERROR`——补丁早就在手里了，
挂掉的是跑测试那一段。重新调一次 AI 有两个后果，每一个都够格否掉这个做法：

1. **这次重试不再是重跑，而是一次新采样。** AI 有随机性，第二次可能交出完全
   不同的补丁。那样"重试"就不是在排除平台抖动，而是在偷偷多采一次样，
   等于变相取最优（C-25 明确禁止）。
2. **白花一次钱。** 真实 Agent 一道题几毛到几块，一次实验几百题。

## 为什么不直接复用 OracleRunner

两者的代码几乎一样，但含义完全不同，合并会污染数据：

- Oracle 交的是**官方补丁**，用来验"harness 不会把正确答案判错"；
- 这个交的是**上一次被测 AI 自己写的补丁**。

真拿 Oracle 去做重放，这条 attempt 的 `agent_name` 会写成 `oracle`，
排行榜上就会出现一个混了官方答案的记录。

## 成本必须报 0

C-56 规定成本要**累计全部 attempt**。重放这一次确实一分钱没花，如实报 0；
把原来那次的成本再报一遍，同一笔钱就被计了两次。

## 补丁从外面喂进来，这里不读制品库

和 Oracle 同一个理由：`app.runner` 在分层里压在 `app.evaluation` 下面，看不见
编排层。而且制品读不出来是**平台的故障**，不该被翻译成"适配器崩了"——
`execute_task_run()` 会把 `run()` 抛的任何异常都记成 `AGENT_RUNTIME_ERROR`，
那是记在被测 AI 头上的。所以读制品这一步放在 Worker 的处理函数里，读不出来
就让整条作业失败重排，一条 attempt 记录都不写。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.runner.adapters.base import PatchSource, as_lookup, sentinel_result
from app.runner.protocol import (
    AgentConfig,
    AgentError,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
)

#: 查不到要重放的补丁时报的错误码。
#: 和 Oracle 的 `gold_patch_missing` 分开，因为排查方向完全不同：
#: 那个是"题库里没有官方补丁"，这个是"上一次的制品读不出来了"。
STORED_PATCH_MISSING = "stored_patch_missing"

#: 写进 `agent_version`。重放没有版本演进，只在协议报文变的时候才动。
STORED_PATCH_VERSION = "1.0"


class StoredPatchRunner:
    """把上一次的标准化补丁原样交出去，一个模型都不调（协议 C-54）。

        runner = StoredPatchRunner({task.task_id: previous_normalized_patch})

    `name` 固定是 `stored-patch`，**不伪装成原来那个 Agent**。报表里能一眼看出
    这条 attempt 没花钱、也没重新采样；写成原 Agent 的名字，看的人会以为
    它又跑了一次真实模型。
    """

    name = "stored-patch"

    def __init__(self, patches: PatchSource | None = None) -> None:
        self._lookup = as_lookup(patches)

    def probe(self) -> ProbeResult:
        """永远可用。补丁在不在是**每道题**的事，探活探不出来。"""
        return ProbeResult(
            ok=True, agent_version=STORED_PATCH_VERSION, detail="补丁重放，无外部依赖"
        )

    def run(self, task: AgentTaskInput, workspace: object, config: AgentConfig) -> AgentRunResult:
        """交出这道题上一次的补丁。

        **不看 deadline，也不看 workspace。** 补丁是现成的字符串，没有任何要花时间
        的工作；把它做成"截止已过就交空手"只会让重试的结果取决于编排层算没算准
        时间，而重试的全部目的正是排除这类抖动。
        """
        started_at = datetime.now(UTC)
        patch = self._lookup(task.task_id)
        if patch is None:
            return sentinel_result(
                agent_name=self.name,
                started_at=started_at,
                exit_code=1,
                error=AgentError(
                    code=STORED_PATCH_MISSING,
                    message=f"没给 {task.task_id} 上一次的补丁，无法按 C-54 重放",
                ),
            )
        return sentinel_result(agent_name=self.name, started_at=started_at, patch=patch)


__all__ = ["STORED_PATCH_MISSING", "STORED_PATCH_VERSION", "StoredPatchRunner"]
