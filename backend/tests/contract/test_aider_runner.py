"""真的把 aider 跑起来，过一遍六条契约（E3-T4）。

**这一组会花钱，而且不进 CI。** 标了 `agent`，`make check` 和 `make test-docker`
都排掉它，只有手动 `make test-agent` 才跑。六条里有三条会真的调模型
（第 2、5、6 条各跑一次），一轮下来在 DeepSeek 上是几分钱的量级。

## 为什么必须有这一组

前面两层测的都是"我们自己的代码对不对"：

- `tests/unit/test_aider_output.py`：从一段**事先写好**的 stdout 里抠数字
- `tests/sandbox/test_aider_runner.py`：容器换成假的，验补丁怎么抓、故障怎么归类

它们有一个共同的盲区：**那段 stdout 是我们自己编的。** aider 真实打出来的是不是
长那样，只有真跑一次才知道。这一组就是用来关掉这个盲区的 ——
它一旦变红，多半是 aider 换了输出格式，而那会让成本统计悄悄变成空值。

## 第 4 条为什么跳过

第 4 条要一个"会去改指定文件"的适配器。没法命令 aider 去改某个具体文件 ——
真要试，就得在提示词里写"请修改 tests/test_password.py"，那测的是提示词，
不是适配器。套件本来就允许给不出来时跳过（`runner_that_edits` 返回 None）。
受保护路径那条线在 `tests/sandbox/test_aider_runner.py` 里用假容器验过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.config import Settings, get_settings
from app.runner.adapters.aider import DEFAULT_AIDER_IMAGE, AiderRunner
from app.runner.protocol import AgentConfig, AgentRunner, AgentTaskInput
from app.sandbox.container import build_env
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks
from tests.contract.runner_contract import AgentRunnerContract

pytestmark = [pytest.mark.docker, pytest.mark.agent, pytest.mark.slow]

#: 拿哪道题来跑。第一道是 auth（空口令能登录），题干里点名了具体的模块和函数，
#: 对第一个真实 Agent 来说是个公道的起点。
TASK = load_tasks()[0]


@pytest.fixture(scope="module")
def settings() -> Settings:
    config = get_settings()
    if not config.agent_env_for(TASK.task_id) and not config.deepseek_api_key:
        pytest.skip("没配 DEEPSEEK_API_KEY，跳过真实 Agent 的契约测试")
    return config


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("aider-contract-mirrors")
    build(mirror_root=root)
    return root


class TestAiderRunner(AgentRunnerContract):
    """六条契约，跑在真容器 + 真模型上。"""

    #: 真实 CLI 要起容器、装载自己、扫一遍仓库、再等模型回话。
    #: 默认的 60 秒只够走到一半，第 2 条会因为超时拿到空补丁而变红。
    task_deadline_s = 900.0

    #: 截止已过时适配器根本不起容器，所以收尾其实是毫秒级的。
    #: 留 30 秒是套件的默认值，够宽松了。
    deadline_grace_s = 30.0

    @pytest.fixture
    def runner(self) -> AgentRunner:
        """不传 model：让它走 `AgentTaskInput.model.name`，也就是生产路径。

        构造参数里那个 `model` 是给"任务输入带的是假模型名"的场合用的
        （套件自带的合成任务就是那种）。这里我们给的是真题，模型名从
        `agent_configs.model_name` 那条路来，正好把生产路径一起验了。
        """
        return AiderRunner()

    @pytest.fixture
    def config(self, settings: Settings, tmp_path: Path) -> AgentConfig:
        """密钥过一遍 `build_env()` 的白名单，和 Worker 里的走法完全一致。"""
        return AgentConfig(
            image=DEFAULT_AIDER_IMAGE,
            env=build_env(settings.agent_env_for(_model())),
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
        """用真题，不用套件自带的合成任务。

        合成任务说的是 `example/demo` 仓库里的 `login()`，而工作区物化出来的是
        golden 的 auth 仓库。aider 会照着题干去找那个函数，找不到就什么都不改，
        第 2 条于是以"没产出补丁"变红 —— 那测出来的是任务和工作区对不上。
        """
        return TASK.agent_task_input(deadline_unix_ms=deadline_ms, model=_model())


def _model() -> str:
    """这一组用哪个模型。和 `cli/seed.py` 里 `aider@deepseek-chat` 那份配置一致。"""
    return "deepseek/deepseek-chat"
