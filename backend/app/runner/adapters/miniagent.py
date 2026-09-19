"""自研 MiniAgent 适配器：复用沙箱、原始补丁捕获和集中失败判据（E3-T6）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.cost import estimate_cost_usd
from app.domain.enums import CostSource
from app.runner.adapters.cli_text import shared_failure
from app.runner.adapters.prompt import build_task_prompt
from app.runner.patch import capture_workspace_diff
from app.runner.protocol import (
    AGENT_STDERR_FILENAME,
    AGENT_STDOUT_FILENAME,
    AGENT_TRAJECTORY_FILENAME,
    DEADLINE_EXCEEDED,
    OOM_KILLED,
    RUNTIME_ERROR,
    AgentConfig,
    AgentError,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
    TokenUsage,
)
from app.sandbox.container import (
    BindMount,
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    Stage,
    agent_limits,
    get_docker_client,
    run_in_container,
)

#: 只要 Python 标准库；复用现有底座镜像，不安装任何 Agent 第三方依赖。
DEFAULT_IMAGE = "bench-base:py311"
VERSION = "0.1.0"
RUNTIME_FILE = Path(__file__).resolve().parents[1] / "miniagent_runtime.py"


def parse_events(stdout: str) -> list[dict[str, Any]]:
    """只读取完整 JSONL；被杀时最后一行可能截断，之前的用量仍应保留。"""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type"):
            events.append(event)
    return events


class MiniAgentRunner:
    """四工具 ReAct Agent；配置进入实验 manifest，真实运行按任务指定模型。"""

    name = "miniagent"

    def __init__(
        self, *, params: Mapping[str, Any] | None = None, run_container: Any = None
    ) -> None:
        self.params = dict(params or {})
        self._run_container = run_container or run_in_container
        self.max_turns = int(self.params.get("max_turns", 30))
        self.max_output_tokens = int(self.params.get("max_output_tokens", 2048))
        self.max_tokens_budget = int(self.params.get("max_tokens_budget", 30_000))
        if self.max_tokens_budget < 1:
            raise ValueError("max_tokens_budget 必须为正数")
        self.thinking = self.params.get("thinking")
        if self.thinking not in (None, "enabled", "disabled"):
            raise ValueError("thinking 必须是 enabled 或 disabled")
        if not 1 <= self.max_turns <= 100 or not 1 <= self.max_output_tokens <= 8192:
            raise ValueError("MiniAgent max_turns 必须在 1–100，max_output_tokens 在 1–8192")

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> MiniAgentRunner:
        """Worker 从持久化配置构造，不依赖宿主机隐藏的模型默认值。"""
        return cls(params=params)

    def probe(self) -> ProbeResult:
        """只检查代码和已有镜像，不起容器、不联网调用模型。"""
        image = str(self.params.get("image", DEFAULT_IMAGE))
        try:
            if not RUNTIME_FILE.is_file():
                raise FileNotFoundError(RUNTIME_FILE)
            get_docker_client().images.get(image)
        except Exception as exc:
            return ProbeResult(ok=False, detail=f"MiniAgent 不可用：{type(exc).__name__}")
        return ProbeResult(ok=True, agent_version=VERSION, detail=f"{image} 与运行代码就绪")

    def run(self, task: AgentTaskInput, workspace: Any, config: AgentConfig) -> AgentRunResult:
        """容器结束后无论成功、超时或故障，都捕获原始 diff 和已落下的轨迹。"""
        started = datetime.now(UTC)
        if task.constraints.remaining_ms() < 1000:
            return AgentRunResult(
                agent_name=self.name,
                agent_version=VERSION,
                model=task.model.name,
                started_at=started,
                finished_at=started,
                duration_ms=0,
                error=AgentError(code=DEADLINE_EXCEEDED, message="启动前截止时间已到"),
            )
        model = task.model.name.removeprefix("deepseek/").removeprefix("openai/")
        is_deepseek = "deepseek" in task.model.name.lower()
        settings = {
            "prompt": build_task_prompt(task),
            "model": model,
            "temperature": task.model.temperature,
            "max_turns": self.max_turns,
            "max_output_tokens": self.max_output_tokens,
            "deadline_unix_ms": task.constraints.deadline_unix_ms,
            "max_tokens_budget": min(
                self.max_tokens_budget,
                task.constraints.max_tokens_budget or self.max_tokens_budget,
            ),
            "thinking": self.thinking,
            "base_url": self.params.get(
                "base_url",
                "https://api.deepseek.com" if is_deepseek else "https://api.openai.com/v1",
            ),
            "key_env": self.params.get(
                "key_env", "DEEPSEEK_API_KEY" if is_deepseek else "OPENAI_API_KEY"
            ),
        }
        spec = ContainerSpec(
            image=config.image or str(self.params.get("image", DEFAULT_IMAGE)),
            command=[
                "python3",
                "-I",
                "-u",
                "/opt/miniagent.py",
                json.dumps(settings, ensure_ascii=False),
            ],
            timeout_s=max(1, task.constraints.remaining_ms() // 1000),
            stage=Stage.AGENT,
            network=NetworkMode.BRIDGE if task.constraints.allow_network else NetworkMode.NONE,
            mounts=(
                BindMount.workspace(Path(workspace.path)),
                BindMount(RUNTIME_FILE, "/opt/miniagent.py", read_only=True),
            ),
            workdir="/workspace",
            env=config.env,
            limits=agent_limits(cpus=config.cpus, memory_mb=config.memory_mb),
            run_id=task.task_id,
        )
        container = self._run_container(spec)
        events = parse_events(container.stdout)
        usage_events = [e for e in events if e["type"] == "llm_usage"]
        usage = (
            TokenUsage(
                input=sum(e["input"] for e in usage_events),
                output=sum(e["output"] for e in usage_events),
                cache_read=sum(e["cache_read"] for e in usage_events),
            )
            if usage_events
            else None
        )
        cost = None
        # 中断中的请求可能已收费却没收到 usage，不能拿已知部分当完整成本。
        finished = (
            bool(events and events[-1]["type"] == "stop")
            and container.exit_code == 0
            and not container.timed_out
            and not container.oom_killed
        )
        if usage is not None and config.token_prices is not None and finished:
            estimated = estimate_cost_usd(
                tokens_input=usage.input,
                tokens_output=usage.output,
                tokens_cache_read=usage.cache_read,
                prices=config.token_prices,
            )
            cost = float(estimated) if estimated is not None else None
        if cost is not None:
            events.append(
                {
                    "type": "cost_estimate",
                    "ts": datetime.now(UTC).isoformat(),
                    "prices_usd_per_mtok": config.token_prices.as_manifest()
                    if config.token_prices is not None
                    else None,
                    "cost_usd": cost,
                    "price_source": self.params.get("price_source"),
                    "model": task.model.name,
                }
            )
        trajectory = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        uri = None
        if config.artifact_dir is not None:
            config.artifact_dir.mkdir(parents=True, exist_ok=True)
            for name, text in (
                (AGENT_STDOUT_FILENAME, container.stdout),
                (AGENT_STDERR_FILENAME, container.stderr),
                (AGENT_TRAJECTORY_FILENAME, trajectory),
            ):
                (config.artifact_dir / name).write_text(text, encoding="utf-8")
            uri = (config.artifact_dir / AGENT_TRAJECTORY_FILENAME).as_uri()
        finished_at = datetime.now(UTC)
        return AgentRunResult(
            agent_name=self.name,
            agent_version=VERSION,
            model=task.model.name,
            started_at=started,
            finished_at=finished_at,
            duration_ms=int((finished_at - started).total_seconds() * 1000),
            exit_code=container.exit_code,
            patch=capture_workspace_diff(workspace),
            patch_source="git_diff",
            token_usage=usage,
            cost_usd=cost,
            cost_source=CostSource.ESTIMATED if cost is not None else CostSource.UNAVAILABLE,
            turns=len(usage_events),
            trajectory_uri=uri,
            error=error_for(container, events),
            raw_stdout_bytes=len(container.stdout.encode()),
            raw_stderr_bytes=len(container.stderr.encode()),
        )


def error_for(container: ContainerResult, events: list[dict[str, Any]]) -> AgentError | None:
    """OOM 优先于超时；公共失败判据只看运行器错误事件，模型对话不能冒充错误。"""
    if container.oom_killed:
        return AgentError(code=OOM_KILLED, message="MiniAgent 容器内存超限")
    if container.timed_out or any(
        e.get("reason") == "deadline" for e in events if e["type"] == "stop"
    ):
        return AgentError(code=DEADLINE_EXCEEDED, message="MiniAgent 到达截止时间")
    errors = [e for e in events if e["type"] == "error"]
    if container.exit_code == 0 and not errors and any(e["type"] == "stop" for e in events):
        return None
    # 非错误事件不送入关键词判据；保留“曾有输出”这个事实用于 137 判据。
    text = "\n".join(json.dumps(e) for e in errors)
    checked = replace(container, stdout=text or ("[output present]" if container.stdout else ""))
    return shared_failure(checked) or AgentError(
        code=RUNTIME_ERROR, message="MiniAgent 未正常完成，详见轨迹"
    )
