"""Agent 容器该接哪个网络——三个真实适配器共用的一条规则（E2-T4）。

    题目不允许联网                     → NONE
    允许联网、harness 给了白名单网络   → EGRESS（只能经代理访问名单里的域名）
    允许联网、没给白名单网络           → BRIDGE（直连整个互联网，只准开发机调试）

单独放一个文件，是为了让"退回 BRIDGE"这个决定只有一处：2026-09-22 查实
claude-code 在 BRIDGE 下 `curl` 到了上游修复的 diff（`05-sandbox.md` §10.5），
再有适配器各写各的就又会有一个漏掉。
"""

from __future__ import annotations

from app.runner.protocol import AgentConfig, AgentTaskInput
from app.sandbox.container import NetworkMode


def agent_network(task: AgentTaskInput, config: AgentConfig) -> tuple[NetworkMode, str | None]:
    """返回 `(network, network_name)`，直接展开进 `ContainerSpec`。"""
    if not task.constraints.allow_network:
        return NetworkMode.NONE, None
    if config.egress_network:
        return NetworkMode.EGRESS, config.egress_network
    return NetworkMode.BRIDGE, None
