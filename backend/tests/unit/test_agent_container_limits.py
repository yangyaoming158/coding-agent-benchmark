"""Agent 阶段容器的限额（E9-T2）。

以前这两个数是**捡来的**：`aider.py` / `claude_code.py` 的 `_spec()` 不传 `limits`，
于是吃 `ResourceLimits()` 按测试容器定的 1536 MB。没人选过它，而内存账里它是大头 ——
`worker_slots=8` 的最坏情况是 8 个容器同时在，其中几个正是 Agent 容器
（`01-requirements.md` §4.6 那笔 `5 × 1.5 GB` 只数了测试容器）。

这一组钉住那条通路：配置 → `AgentConfig` → 适配器 → `ContainerSpec.limits`。
不起容器，只看规格对象。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.domain.capacity import DEFAULT_AGENT_MEMORY_MB, DEFAULT_SANDBOX_MEMORY_MB
from app.runner.adapters.aider import AiderRunner
from app.runner.adapters.claude_code import ClaudeCodeRunner
from app.runner.protocol import AgentConfig
from app.sandbox.container import ContainerSpec
from tests.contract.runner_contract import make_task_input


class _Workspace:
    """`_spec()` 只用 `workspace.path`，不需要真物化一份代码树。"""

    path = Path("/tmp/bench-not-a-real-workspace")


def _spec_of(runner: Any, config: AgentConfig) -> ContainerSpec:
    task = make_task_input(deadline_ms=1_800_000)
    spec: ContainerSpec = runner._spec(task, _Workspace(), config, timeout_s=600)
    return spec


@pytest.fixture(params=["aider", "claude-code"])
def runner(request: pytest.FixtureRequest) -> Any:
    return AiderRunner() if request.param == "aider" else ClaudeCodeRunner()


def test_the_agent_container_gets_the_configured_limits(runner: Any) -> None:
    """配置里的两个数原样落到容器规格上。"""
    spec = _spec_of(runner, AgentConfig(memory_mb=777, cpus=0.5))
    assert spec.limits.memory_mb == 777
    assert spec.limits.cpus == 0.5


def test_without_config_it_falls_back_to_the_agent_default_not_the_sandbox_one(
    runner: Any,
) -> None:
    """没给配置时用 Agent 阶段自己的默认值，**不是**测试容器那个 1536。

    这条是这张卡的起点：以前这里落的就是 1536。
    """
    spec = _spec_of(runner, AgentConfig())
    assert spec.limits.memory_mb == DEFAULT_AGENT_MEMORY_MB
    assert spec.limits.memory_mb != DEFAULT_SANDBOX_MEMORY_MB


def test_the_worker_passes_the_configured_limits_down() -> None:
    """通路的最后一段：Worker 从配置里取，塞进 `AgentConfig`。

    断的话表现是"改了 `AGENT_MEMORY_MB` 没反应" —— 配置在，但没人读它。
    """
    from app.infrastructure.config import Settings
    from app.worker.handlers.eval_task import _agent_config

    settings = Settings(agent_memory_mb=900, agent_cpus=1.5)

    class _Ctx:
        pass

    class _Loaded:
        agent_params: ClassVar[dict[str, Any]] = {}
        model_name = "none"
        token_prices = None

    ctx = _Ctx()
    ctx.settings = settings  # type: ignore[attr-defined]
    config = _agent_config(ctx, _Loaded())  # type: ignore[arg-type]
    assert config.memory_mb == 900
    assert config.cpus == 1.5
