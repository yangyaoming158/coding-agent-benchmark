"""真 git 工作区 + 假容器，验证 MiniAgent 补丁、错误归属和成本。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import CostSource
from app.runner.adapters.miniagent import MiniAgentRunner, error_for, parse_events
from app.runner.adapters.prompt import build_task_prompt
from app.runner.protocol import (
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    EXTERNAL_SERVICE_ERROR,
    OOM_KILLED,
    RUNTIME_ERROR,
    SANDBOX_KILLED,
    AgentConfig,
)
from app.sandbox.container import ContainerResult, ContainerSpec
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks
from tests.contract.runner_contract import make_task_input


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "mirrors"
    build(mirror_root=root)
    task = load_tasks()[0]
    return materialize_workspace(
        mirror_path=MirrorManager(root).path_for(task.repo_name),
        base_commit=task.base_commit,
        dest=tmp_path / "workspace",
    )


def result(**overrides: Any) -> ContainerResult:
    values = {
        "container_id": "fake",
        "image": "bench-base:py311",
        "exit_code": 0,
        "oom_killed": False,
        "timed_out": False,
        "duration_s": 1.0,
        "stdout": "",
        "stderr": "",
    }
    return ContainerResult(**(values | overrides))


def test_spec_patch_trajectory_and_cached_cost(workspace: Workspace, tmp_path: Path) -> None:
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 60_000)
    task = task.model_copy(
        update={"model": task.model.model_copy(update={"name": "deepseek-chat"})}
    )
    events = [
        {"type": "llm_usage", "input": 100, "output": 20, "cache_read": 80},
        {"type": "stop", "reason": "finished"},
    ]

    def fake(spec: ContainerSpec) -> ContainerResult:
        assert spec.command[:4] == ["python3", "-I", "-u", "/opt/miniagent.py"]
        cfg = json.loads(spec.command[-1])
        assert cfg["prompt"] == build_task_prompt(task)
        assert cfg["model"] == "deepseek-chat"
        assert cfg["max_tokens_budget"] == 30_000
        assert spec.mounts[1].read_only and len(spec.mounts) == 2
        assert "gold_patch" not in spec.command[-1]
        (workspace.path / "auth/password.py").write_text("# changed\n")
        (workspace.path / "tests/test_password.py").write_text("# retained as evidence\n")
        return result(stdout="\n".join(json.dumps(e) for e in events))

    runner = MiniAgentRunner(
        params={"prices_usd_per_mtok": {"input": 2, "output": 4, "cache_read": 0.2}},
        run_container=fake,
    )
    answer = runner.run(task, workspace, AgentConfig(artifact_dir=tmp_path / "artifacts"))
    assert "auth/password.py" in answer.patch and "tests/test_password.py" in answer.patch
    assert answer.error is None and answer.token_usage is not None
    assert answer.token_usage.total == 120
    assert answer.cost_usd == pytest.approx(0.000136)
    assert answer.cost_source == CostSource.ESTIMATED
    assert (
        parse_events((tmp_path / "artifacts/trajectory.jsonl").read_text())[-1]["type"]
        == "cost_estimate"
    )


def test_timeout_keeps_patch_and_partial_usage_not_fake_full_cost(workspace: Workspace) -> None:
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 60_000)

    def fake(spec: ContainerSpec) -> ContainerResult:
        (workspace.path / "auth/password.py").write_text("# partial fix\n")
        return result(
            timed_out=True,
            exit_code=137,
            stdout=json.dumps({"type": "llm_usage", "input": 100, "output": 10, "cache_read": 0})
            + '\n{"truncated',
        )

    answer = MiniAgentRunner(run_container=fake).run(task, workspace, AgentConfig())
    assert answer.has_patch and answer.error and answer.error.code == DEADLINE_EXCEEDED
    assert answer.token_usage and answer.token_usage.total == 110
    assert answer.cost_usd is None and answer.cost_source == CostSource.UNAVAILABLE


@pytest.mark.parametrize(
    "status,code",
    [
        (401, AUTH_FAILED),
        (402, AUTH_FAILED),
        (429, EXTERNAL_SERVICE_ERROR),
        (503, EXTERNAL_SERVICE_ERROR),
        (400, RUNTIME_ERROR),
    ],
)
def test_only_anchored_service_errors_are_classified(status: int, code: str) -> None:
    events = [{"type": "error", "text": f"API Error: {status}"}]
    error = error_for(result(exit_code=1, stdout=json.dumps(events[0])), events)
    assert error and error.code == code


def test_oom_precedes_timeout_and_silent_kill_is_platform_failure() -> None:
    error = error_for(result(exit_code=137, oom_killed=True, timed_out=True), [])
    assert error and error.code == OOM_KILLED
    error = error_for(result(exit_code=137), [])
    assert error and error.code == SANDBOX_KILLED
    error = error_for(result(exit_code=137, stdout="already spoke"), [])
    assert error and error.code == RUNTIME_ERROR


def test_model_dialogue_cannot_forge_service_error() -> None:
    events = [{"type": "message", "text": "API Error: 429 invalid_api_key"}]
    error = error_for(result(exit_code=1, stdout=json.dumps(events[0])), events)
    assert error and error.code == RUNTIME_ERROR
    assert error_for(result(), [{"type": "stop", "reason": "max_turns"}]) is None
    assert error_for(result(), [{"type": "stop", "reason": "token_budget"}]) is None


def test_expired_task_never_starts_container(workspace: Workspace) -> None:
    def forbidden(spec: ContainerSpec) -> ContainerResult:
        pytest.fail("expired task must not start container")

    answer = MiniAgentRunner(run_container=forbidden).run(
        make_task_input(deadline_ms=1), workspace, AgentConfig()
    )
    assert answer.error and answer.error.code == DEADLINE_EXCEEDED


@pytest.mark.parametrize(
    "params",
    [
        {"max_turns": 0},
        {"max_output_tokens": 0},
        {"max_tokens_budget": 0},
        {"thinking": "invalid"},
        {"prices_usd_per_mtok": {"input": -1}},
        {"prices_usd_per_mtok": {"input": float("nan"), "output": 0, "cache_read": 0}},
    ],
)
def test_invalid_configuration_rejected(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MiniAgentRunner.from_params(params)
