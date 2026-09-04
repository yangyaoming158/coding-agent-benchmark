"""Noop 哨兵：永远交空补丁（E3-T2）。

它是解决率的**下界探针**。在一个健康的题库上，空补丁的解决率必须是 **0%**
（协议 C-50）。不是 0% 只有一个解释：有的题在修复前 F2P 就已经通过了，
那道题不用改代码也算"解决"，整个排行榜的下限就不可信了。

Noop 交空补丁**不是失败**，是它的定义。所以 `error` 留空、`exit_code` 是 0——
把它标成失败的话，这次运行会被算进"平台故障"，而平台什么问题都没有。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.runner.adapters.base import sentinel_result
from app.runner.protocol import AgentConfig, AgentRunResult, AgentTaskInput, ProbeResult


class NoopRunner:
    """什么都不做的哨兵。"""

    name = "noop"

    def probe(self) -> ProbeResult:
        """永远可用：它不依赖任何外部东西，没有能坏掉的地方。"""
        return ProbeResult(ok=True, agent_version="1.0", detail="Noop 哨兵，无外部依赖")

    def run(self, task: AgentTaskInput, workspace: object, config: AgentConfig) -> AgentRunResult:
        """立刻返回一个空补丁。

        `task` / `workspace` / `config` 一个都不看，这是刻意的：Noop 的输出必须
        **完全不依赖输入**，否则它就不再是一条稳定的基线了。deadline 也不看——
        它本来就一秒都不花。
        """
        return sentinel_result(agent_name=self.name, started_at=datetime.now(UTC))


__all__ = ["NoopRunner"]
