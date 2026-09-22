"""ClaudeCodeRunner 在真工作区上的行为（E3-T5）。

这一组**真的物化工作区、真的跑 git diff**，但**不起容器、不调模型**：容器那一步
换成一个假的 `run_container`，它按脚本改几个文件、返回一段事先写好的 stream-json。

为什么这么切（和 `test_aider_runner.py` 同一个理由）：适配器里真正容易出错的不是
"能不能把 docker 拉起来"（那是沙箱层的事，E2-T2 已经验过），而是**容器结束之后的
那几步** —— 补丁从哪抓、超时了还抓不抓、用量怎么读、故障怎么归类。这些都需要一个
真的 git 工作区才测得出来，但一个都不需要真的 claude。

真把 claude 跑起来的是 `tests/contract/test_claude_code_runner.py`（要 Key、花钱）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import CostSource
from app.runner.adapters.claude_code import (
    DEFAULT_MAX_TURNS,
    ClaudeCodeRunner,
)
from app.runner.protocol import (
    AGENT_STDOUT_FILENAME,
    AGENT_TRAJECTORY_FILENAME,
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    OOM_KILLED,
    RUNTIME_ERROR,
    AgentConfig,
    AgentRunResult,
    AgentTaskInput,
)
from app.sandbox.container import (
    WORKSPACE_TARGET,
    ContainerResult,
    ContainerSpec,
    NetworkMode,
    Stage,
)
from app.sandbox.mirror import MirrorManager
from app.sandbox.workspace import Workspace, materialize_workspace
from cli.golden import build, load_tasks
from tests.contract.runner_contract import make_task_input

TASK = load_tasks()[0]

#: 这道题的被测源文件，"claude" 会去改它。
SOURCE_FILE = "auth/password.py"
#: 测试文件。第 4 条契约要的是：改了也**留在原始补丁里**，剔除是平台的事。
CHEAT_FILE = "tests/test_password.py"

#: 假 Key。**拼出来而不是整串写死** —— 整串写会被提交前的密钥扫描器拦下。
FAKE_KEY = "sk-" + "test" + "0" * 20


def line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def success_stdout(*, cost: float = 0.0421, version: str = "2.1.236") -> str:
    """一次正常跑完长什么样：init → 想一下 → 读文件 → 改文件 → result。"""
    return (
        line(
            {
                "type": "system",
                "subtype": "init",
                "cwd": WORKSPACE_TARGET,
                "session_id": "s1",
                "model": "deepseek-chat",
                "version": version,
                "permissionMode": "bypassPermissions",
            }
        )
        + line(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "先看看这个函数"}],
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 18,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 4000,
                    },
                },
            }
        )
        + line(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Edit",
                            "input": {"file_path": f"{WORKSPACE_TARGET}/{SOURCE_FILE}"},
                        }
                    ],
                    "usage": {
                        "input_tokens": 40,
                        "output_tokens": 260,
                        "cache_read_input_tokens": 4000,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        )
        + line(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    ],
                },
            }
        )
        + line(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 4,
                "duration_ms": 30123,
                "total_cost_usd": cost,
                "session_id": "s1",
            }
        )
    )


SUCCESS_STDOUT = success_stdout()


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """现建一套 golden 镜像，不动开发机上 `var/mirrors` 里那份。"""
    root = tmp_path_factory.mktemp("claude-mirrors")
    build(mirror_root=root)
    return root


@pytest.fixture
def workspace(mirror_root: Path, tmp_path: Path) -> Workspace:
    return materialize_workspace(
        mirror_path=MirrorManager(mirror_root).path_for(TASK.repo_name),
        base_commit=TASK.base_commit,
        dest=tmp_path / "agent",
    )


@pytest.fixture
def task() -> AgentTaskInput:
    """一份还剩十分钟的任务。够长，不会撞上"截止已过就别起容器"那条短路。"""
    return make_task_input(deadline_ms=int(time.time() * 1000) + 600_000)


def container_result(
    *,
    stdout: str = SUCCESS_STDOUT,
    stderr: str = "",
    exit_code: int = 0,
    oom_killed: bool = False,
    timed_out: bool = False,
) -> ContainerResult:
    return ContainerResult(
        container_id="fake",
        image="bench-agent:py311-claude-code",
        exit_code=exit_code,
        oom_killed=oom_killed,
        timed_out=timed_out,
        duration_s=1.0,
        stdout=stdout,
        stderr=stderr,
    )


class FakeContainer:
    """替掉 `run_in_container`：按脚本改工作区，返回事先写好的结果。

    顺手把 `ContainerSpec` 记下来 —— 挂载点、网络模式、注进去的环境变量这些
    不看一眼的话，等到真跑起来才发现挂错目录，一次调试要几分钟起步。
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        result: ContainerResult | None = None,
        edits: dict[str, str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.result = result or container_result()
        self.edits = edits if edits is not None else {SOURCE_FILE: "# 被 claude 改过\n"}
        self.spec: ContainerSpec | None = None
        self.calls = 0

    def __call__(self, spec: ContainerSpec) -> ContainerResult:
        self.calls += 1
        self.spec = spec
        for relative, content in self.edits.items():
            path = self.workspace.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return self.result


def run_with(
    workspace: Workspace,
    task: AgentTaskInput,
    *,
    result: ContainerResult | None = None,
    edits: dict[str, str] | None = None,
    config: AgentConfig | None = None,
    base_url: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> tuple[FakeContainer, AgentRunResult]:
    fake = FakeContainer(workspace, result=result, edits=edits)
    runner = ClaudeCodeRunner(
        model="deepseek-chat", base_url=base_url, max_turns=max_turns, run_container=fake
    )
    return fake, runner.run(task, workspace, config or AgentConfig())


# ── 补丁 ────────────────────────────────────────────────────


def test_patch_comes_from_the_workspace(workspace: Workspace, task: AgentTaskInput) -> None:
    """协议 §9.1 的 workspace-mutation 模式：AI 改文件，补丁由我们 git diff 出来。"""
    _, outcome = run_with(workspace, task)
    assert outcome.has_patch
    assert SOURCE_FILE in outcome.patch
    assert outcome.patch_source == "git_diff"


def test_the_diff_is_taken_against_base_sha_not_head(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """claude 默认不提交，但它能用 Bash 工具自己 commit。

    真发生的话，裸 `git diff` 会是空的 —— 补丁必须以 base_sha 为基准。
    """
    from app.sandbox.git_cli import run_git

    def commit_like_a_disobedient_agent(spec: ContainerSpec) -> ContainerResult:
        path = workspace.path / SOURCE_FILE
        path.write_text("# 改完还自己提交了\n", encoding="utf-8")
        run_git(["add", "-A"], cwd=workspace.path)
        run_git(["commit", "-m", "fix"], cwd=workspace.path)
        return container_result()

    runner = ClaudeCodeRunner(model="m", run_container=commit_like_a_disobedient_agent)
    outcome = runner.run(task, workspace, AgentConfig())
    assert outcome.has_patch and SOURCE_FILE in outcome.patch


def test_protected_path_edits_stay_in_the_raw_patch(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """契约第 4 条：适配器交原始 diff，剔除是平台的事（C-08b、C-41）。

    适配器自己先过滤的话，"AI 试图改测试文件"这条证据就没了。
    """
    _, outcome = run_with(
        workspace,
        task,
        edits={SOURCE_FILE: "# 改了源码\n", CHEAT_FILE: "# 顺手把测试也改了\n"},
    )
    assert CHEAT_FILE in outcome.patch


# ── 用量 ────────────────────────────────────────────────────


def test_token_and_cost_land_in_the_result(workspace: Workspace, task: AgentTaskInput) -> None:
    """直连官方端点时 `total_cost_usd` 可信，照抄。"""
    _, outcome = run_with(workspace, task)
    assert outcome.token_usage is not None
    # 两条消息：(120+4000) + (40+4000)，缓存那部分算在 input 里
    assert outcome.token_usage.input == 8160
    assert outcome.token_usage.output == 278
    assert outcome.token_usage.cache_read == 4000
    assert outcome.cost_usd == 0.0421
    assert outcome.cost_source is CostSource.REPORTED
    assert outcome.turns == 4


def test_a_gateway_run_reports_cost_as_unavailable(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """走中转端点时那个成本是 CLI 拿自己的价目表算的，认不出模型就是 0。

    宁可报 unavailable 让平台去估，也不能把这个 0 当成"真没花钱"（协议纪律 3）。
    """
    _, outcome = run_with(
        workspace,
        task,
        result=container_result(stdout=success_stdout(cost=0.0)),
        base_url="https://gw",
    )
    assert outcome.cost_usd is None
    assert outcome.cost_source is CostSource.UNAVAILABLE
    # token 还在 —— 不可信的只有钱
    assert outcome.token_usage is not None and outcome.token_usage.input == 8160


def test_unreadable_usage_is_reported_as_unavailable_not_zero(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    _, outcome = run_with(workspace, task, result=container_result(stdout="没有一行是 JSON\n"))
    assert outcome.token_usage is None
    assert outcome.cost_usd is None
    assert outcome.cost_source is CostSource.UNAVAILABLE


def test_version_is_read_from_the_init_event(workspace: Workspace, task: AgentTaskInput) -> None:
    """镜像里钉的版本只是兜底，现场报的才是事实。"""
    _, outcome = run_with(
        workspace, task, result=container_result(stdout=success_stdout(version="9.9.9"))
    )
    assert outcome.agent_version == "9.9.9"


# ── 故障归类 ────────────────────────────────────────────────


def test_timeout_still_keeps_the_patch(workspace: Workspace, task: AgentTaskInput) -> None:
    """协议 C-09a：超时也要保存补丁。改了一半的补丁照样送去判定。"""
    _, outcome = run_with(workspace, task, result=container_result(exit_code=137, timed_out=True))
    assert outcome.has_patch
    assert outcome.error is not None and outcome.error.code == DEADLINE_EXCEEDED


def test_oom_is_not_reported_as_a_timeout(workspace: Workspace, task: AgentTaskInput) -> None:
    """两种情况的退出码都是 137。判反了：OOM 该降配重试，超时该直接判没修好。"""
    _, outcome = run_with(
        workspace,
        task,
        result=container_result(exit_code=137, oom_killed=True, timed_out=True),
    )
    assert outcome.error is not None and outcome.error.code == OOM_KILLED


def test_auth_failure_is_told_apart_from_a_crash(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """判错的代价不对称：鉴权失败按 C-18 重试 3 次，运行时错误只重试 1 次。

    一个配错的 Key 被记成运行时错误的话，解决率会安静地掉到 0。
    """
    stderr = 'API Error: 401 {"error":{"message":"invalid api key"}}'
    _, outcome = run_with(
        workspace, task, result=container_result(stdout="", stderr=stderr, exit_code=1)
    )
    assert outcome.error is not None and outcome.error.code == AUTH_FAILED


def test_running_out_of_turns_is_not_a_failure(workspace: Workspace, task: AgentTaskInput) -> None:
    """预算用完 = "在给定预算内没修完"，和"改错了"同一类，该交给 Judge 判。

    照 `is_error` 的字面判成故障的话，会触发重试、白花一次钱，归因也会指错方向。
    """
    stdout = success_stdout().replace(
        '"subtype": "success", "is_error": false',
        '"subtype": "error_max_turns", "is_error": true',
    )
    assert "error_max_turns" in stdout, "锚点没替换上，用例会失去意义"
    _, outcome = run_with(workspace, task, result=container_result(stdout=stdout, exit_code=1))
    assert outcome.error is None
    assert outcome.has_patch


def test_a_missing_result_event_is_a_failure_even_with_exit_zero(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """正常跑完一定有 result 那一行。没有它说明崩在了半路。

    这时候哪怕退出码是 0，也不能把"工作区没改动"记成"AI 没修好" ——
    那会把一次平台故障算进被测 AI 的解决率里。
    """
    stdout = "".join(SUCCESS_STDOUT.splitlines(keepends=True)[:-1])
    _, outcome = run_with(
        workspace, task, result=container_result(stdout=stdout, exit_code=0), edits={}
    )
    assert outcome.error is not None and outcome.error.code == RUNTIME_ERROR
    assert "result 事件" in outcome.error.message


def test_an_empty_patch_with_a_clean_result_is_not_a_failure(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """ "AI 没改出东西"是它自己的问题，对应 UNRESOLVED，不是平台故障。

    在这里报错的话，一次正常的"没修好"会触发重试，白花钱还把归因引向错误的方向。
    """
    _, outcome = run_with(workspace, task, edits={})
    assert outcome.error is None
    assert not outcome.has_patch


def test_error_codes_are_the_canonical_ones(workspace: Workspace, task: AgentTaskInput) -> None:
    """评测单元靠这几个码翻译 `infra_outcome`，按子串猜是不可靠的。"""
    _, outcome = run_with(workspace, task, result=container_result(exit_code=1, stdout="boom"))
    assert outcome.error is not None
    assert outcome.error.code in {DEADLINE_EXCEEDED, AUTH_FAILED, RUNTIME_ERROR, OOM_KILLED}


# ── 截止时刻 ────────────────────────────────────────────────


def test_a_passed_deadline_does_not_start_a_container(workspace: Workspace) -> None:
    """契约第 3 条要的"优雅返回、不留孤儿进程"，最干净的实现是根本没起过东西。"""
    task = make_task_input(deadline_ms=int(time.time() * 1000) - 1000)
    fake, outcome = run_with(workspace, task)
    assert fake.calls == 0
    assert outcome.error is not None and outcome.error.code == DEADLINE_EXCEEDED


def test_a_deadline_too_close_to_be_useful_also_short_circuits(workspace: Workspace) -> None:
    """起容器、拉起 node、装载 CLI 本身就要十几秒，剩几秒钟只会白花这段时间。"""
    task = make_task_input(deadline_ms=int(time.time() * 1000) + 5_000)
    fake, _ = run_with(workspace, task)
    assert fake.calls == 0


# ── 容器规格 ────────────────────────────────────────────────


def test_container_spec_matches_the_agent_stage(workspace: Workspace, task: AgentTaskInput) -> None:
    fake, _ = run_with(workspace, task)
    assert fake.spec is not None
    assert fake.spec.stage is Stage.AGENT
    assert fake.spec.workdir == WORKSPACE_TARGET
    assert fake.spec.mounts[0].source == workspace.path
    assert fake.spec.run_id == task.task_id


def test_agent_stage_has_network_but_only_when_the_task_allows_it(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """被测 AI 要连大模型 API，所以 Agent 阶段联网；测试阶段永远断网（C-31）。"""
    fake, _ = run_with(workspace, task)
    assert fake.spec is not None and fake.spec.network is NetworkMode.BRIDGE

    offline = task.model_copy(
        update={"constraints": task.constraints.model_copy(update={"allow_network": False})}
    )
    fake, _ = run_with(workspace, offline)
    assert fake.spec is not None and fake.spec.network is NetworkMode.NONE

    # harness 给了出站白名单网络时接它，不再是 BRIDGE（E2-T4）
    fake, _ = run_with(workspace, task, config=AgentConfig(egress_network="bench-egress"))
    assert fake.spec is not None and fake.spec.network is NetworkMode.EGRESS
    assert fake.spec.network_name == "bench-egress"


def test_the_command_carries_the_configured_budget(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    fake, _ = run_with(workspace, task, max_turns=11)
    assert fake.spec is not None
    command = list(fake.spec.command)
    assert command[command.index("--max-turns") + 1] == "11"


def test_the_key_is_renamed_before_it_reaches_the_container(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """`agent_env_for()` 挑出来的是 DeepSeek 的 Key，而 claude 只认 ANTHROPIC_*。"""
    config = AgentConfig(env={"DEEPSEEK_API_KEY": FAKE_KEY, "TZ": "UTC"})
    fake, _ = run_with(workspace, task, config=config, base_url="https://gw/anthropic")
    assert fake.spec is not None
    assert fake.spec.env["ANTHROPIC_AUTH_TOKEN"] == FAKE_KEY
    assert fake.spec.env["ANTHROPIC_BASE_URL"] == "https://gw/anthropic"
    assert "DEEPSEEK_API_KEY" not in fake.spec.env


def test_image_comes_from_the_agent_config(workspace: Workspace, task: AgentTaskInput) -> None:
    """真实评测的镜像来自 `agent_configs.params["image"]`，不是适配器里的默认值。"""
    fake, _ = run_with(workspace, task, config=AgentConfig(image="bench-agent:custom"))
    assert fake.spec is not None and fake.spec.image == "bench-agent:custom"


# ── 制品 ────────────────────────────────────────────────────


def test_side_files_are_written_for_the_harness_to_pick_up(
    workspace: Workspace, task: AgentTaskInput, tmp_path: Path
) -> None:
    """全量 stdout 和轨迹写进 artifact_dir，`execute_task_run()` 跑完来捡。

    **不能写工作区** —— 那里多出来的文件会进 git diff，变成补丁里凭空多出的改动。
    """
    artifact_dir = tmp_path / "agent-io"
    _, outcome = run_with(workspace, task, config=AgentConfig(artifact_dir=artifact_dir))

    assert (artifact_dir / AGENT_STDOUT_FILENAME).exists()
    rows = [
        json.loads(row)
        for row in (artifact_dir / AGENT_TRAJECTORY_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [r["type"] for r in rows if r["type"] == "tool_call"] == ["tool_call"]
    call = next(r for r in rows if r["type"] == "tool_call")
    assert (call["name"], call["summary"], call["ok"]) == ("Edit", SOURCE_FILE, True)
    assert outcome.trajectory_uri is not None
    assert outcome.trajectory_uri.endswith(AGENT_TRAJECTORY_FILENAME)


def test_no_artifact_dir_is_fine(workspace: Workspace, task: AgentTaskInput) -> None:
    _, outcome = run_with(workspace, task, config=AgentConfig())
    assert outcome.trajectory_uri is None


def test_the_workspace_gains_no_stray_files(workspace: Workspace, task: AgentTaskInput) -> None:
    """claude 的配置和会话记录都写在容器的 /tmp（Dockerfile 把 HOME 指过去了）。

    要是漏到工作区里，`.claude/` 会变成补丁里的一处改动。
    """
    _, outcome = run_with(workspace, task, config=AgentConfig())
    assert ".claude" not in outcome.patch
