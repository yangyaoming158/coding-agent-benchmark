"""MiniAgent 真容器与真模型契约；仅手动 agent 测试调用，每轮最多 15000 token。"""

from pathlib import Path

import pytest

from app.infrastructure.config import get_settings
from app.runner.adapters.miniagent import MiniAgentRunner
from app.runner.protocol import AgentConfig, AgentTaskInput
from app.sandbox.container import build_env
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks
from cli.seed import MINIAGENT_PARAMS
from tests.contract.runner_contract import AgentRunnerContract

pytestmark = [pytest.mark.docker, pytest.mark.agent, pytest.mark.slow]
TASK = load_tasks()[0]
MODEL = "deepseek/deepseek-flash"


class TestMiniAgentRunner(AgentRunnerContract):
    """复用六条契约；受保护路径的可控改动在 sandbox 测试中验证。"""

    task_deadline_s = 180.0

    @pytest.fixture
    def runner(self) -> MiniAgentRunner:
        if not get_settings().deepseek_api_key:
            pytest.skip("未配置 DEEPSEEK_API_KEY")
        return MiniAgentRunner(params={**MINIAGENT_PARAMS, "max_tokens_budget": 15_000})

    @pytest.fixture
    def config(self, tmp_path: Path) -> AgentConfig:
        return AgentConfig(
            env=build_env(get_settings().agent_env_for(MODEL)),
            artifact_dir=tmp_path / "agent-io",
        )

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        root = tmp_path / "mirrors"
        build(mirror_root=root)
        return materialize_workspace(
            mirror_path=MirrorManager(root).path_for(TASK.repo_name),
            base_commit=TASK.base_commit,
            dest=tmp_path / "workspace",
        )

    def make_task(self, *, deadline_ms: int) -> AgentTaskInput:
        """题面和物化仓库一致，不能拿合成题面去驱动真实模型。"""
        return TASK.agent_task_input(deadline_unix_ms=deadline_ms, model=MODEL)
