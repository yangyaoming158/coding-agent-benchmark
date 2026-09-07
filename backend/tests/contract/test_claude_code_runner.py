"""真的把 Claude Code 跑起来，过一遍六条契约（E3-T5）。

**这一组会花钱，而且不进 CI。** 标了 `agent`，`make check` 和 `make test-docker`
都排掉它，只有手动 `make test-agent` 才跑。

## 它打的是哪个端点

默认走 **DeepSeek 的 Anthropic 兼容端点**，和 `cli/seed.py` 里
`claude-code@deepseek-chat` 那份配置一致 —— 也就是说这一组同时验了两件事：
适配器本身，以及"claude 这个 CLI + 一个非官方底座模型"这条路走不走得通。
想跑官方端点就配上 `ANTHROPIC_API_KEY` 并把 `BASE_URL` 设成 None。

## 为什么必须有这一组

前面两层测的都是"我们自己的代码对不对"：

- `tests/unit/test_claude_code_output.py`：从**事先写好**的 stream-json 里抠数字
- `tests/sandbox/test_claude_code_runner.py`：容器换成假的，验补丁怎么抓、故障怎么归类

它们有一个共同的盲区：**那段 JSONL 是我们自己编的。** claude 真实打出来的事件
是不是长那样、`--permission-mode bypassPermissions` 在容器里到底认不认，
只有真跑一次才知道。这一组就是用来关掉这个盲区的。

## 第 4 条为什么跳过

第 4 条要一个"会去改指定文件"的适配器。没法命令 claude 去改某个具体文件 ——
真要试，就得在提示词里写"请修改 tests/test_password.py"，那测的是提示词，
不是适配器。套件本来就允许给不出来时跳过（`runner_that_edits` 返回 None）。
受保护路径那条线在 `tests/sandbox/test_claude_code_runner.py` 里用假容器验过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.config import Settings, get_settings
from app.runner.adapters.claude_code import DEFAULT_CLAUDE_CODE_IMAGE, ClaudeCodeRunner
from app.runner.protocol import AgentConfig, AgentRunner, AgentTaskInput
from app.sandbox.container import build_env
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks
from tests.contract.runner_contract import AgentRunnerContract

pytestmark = [pytest.mark.docker, pytest.mark.agent, pytest.mark.slow]

#: 拿哪道题来跑。和 Aider 那组同一道（auth：空口令能登录），
#: 两个 Agent 跑同一道题，结果才有可比性。
TASK = load_tasks()[0]

#: 底座模型和端点。和 `cli/seed.py` 的 `claude-code@deepseek-chat` 保持一致。
MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com/anthropic"


@pytest.fixture(scope="module")
def settings() -> Settings:
    config = get_settings()
    if not config.agent_env_for(MODEL):
        pytest.skip(f"没配 {MODEL} 那一家的 Key，跳过真实 Agent 的契约测试")
    return config


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("claude-contract-mirrors")
    build(mirror_root=root)
    return root


class TestClaudeCodeRunner(AgentRunnerContract):
    """六条契约，跑在真容器 + 真模型上。"""

    #: 真实 CLI 要起容器、装载自己、扫一遍仓库、再等模型回话。
    #: 默认的 60 秒只够走到一半，第 2 条会因为超时拿到空补丁而变红。
    task_deadline_s = 900.0

    #: 截止已过时适配器根本不起容器，所以收尾其实是毫秒级的。
    deadline_grace_s = 30.0

    @pytest.fixture
    def runner(self) -> AgentRunner:
        """不传 model：让它走 `AgentTaskInput.model.name`，也就是生产路径。"""
        return ClaudeCodeRunner(base_url=BASE_URL)

    @pytest.fixture
    def config(self, settings: Settings, tmp_path: Path) -> AgentConfig:
        """密钥过一遍 `build_env()` 的白名单，和 Worker 里的走法完全一致。

        注进去的是 `DEEPSEEK_API_KEY`，改名成 `ANTHROPIC_AUTH_TOKEN` 是适配器
        在 `credential_env()` 里做的 —— 这一步也顺带被这组验了。
        """
        return AgentConfig(
            image=DEFAULT_CLAUDE_CODE_IMAGE,
            env=build_env(settings.agent_env_for(MODEL)),
            artifact_dir=tmp_path / "agent-io",
        )

    @pytest.fixture
    def workspace(self, mirror_root: Path, tmp_path: Path) -> Workspace:
        """每条用例一份新的工作区 —— 上一条跑完里面已经有 AI 的改动了。"""
        return materialize_workspace(
            mirror_path=MirrorManager(mirror_root).path_for(TASK.repo_name),
            base_commit=TASK.base_commit,
            dest=tmp_path / "agent",
        )

    def make_task(self, *, deadline_ms: int) -> AgentTaskInput:
        """用真题，不用套件自带的合成任务（理由见基类那条说明）。"""
        return TASK.agent_task_input(deadline_unix_ms=deadline_ms, model=MODEL)
