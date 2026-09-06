"""AiderRunner 在真工作区上的行为（E3-T4）。

这一组**真的物化工作区、真的跑 git diff**，但**不起容器、不调模型**：容器那一步
换成一个假的 `run_container`，它按脚本改几个文件、返回一段事先写好的 stdout。

为什么这么切：AiderRunner 里真正容易出错的不是"能不能把 docker 拉起来"
（那是沙箱层的事，E2-T2 已经验过），而是**容器结束之后的那几步** ——
补丁从哪抓、超时了还抓不抓、用量怎么读、故障怎么归类。这些都需要一个真的 git
工作区才测得出来，但一个都不需要真的 aider。

所以这一组进快速测试集，每次提交都跑。真把 aider 跑起来的是
`tests/contract/test_aider_runner.py`（要 API Key，手动触发）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.domain.enums import CostSource, InfraOutcome
from app.runner.adapters.aider import AiderRunner
from app.runner.protocol import (
    AGENT_STDOUT_FILENAME,
    AGENT_TRAJECTORY_FILENAME,
    AUTH_FAILED,
    DEADLINE_EXCEEDED,
    OOM_KILLED,
    RUNTIME_ERROR,
    AgentConfig,
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

#: 这道题的被测源文件，"aider" 会去改它。
SOURCE_FILE = "auth/password.py"
#: 测试文件。第 4 条契约要的是：改了也**留在原始补丁里**，剔除是平台的事。
CHEAT_FILE = "tests/test_password.py"

SUCCESS_STDOUT = """\
Aider v0.86.2
Model: deepseek/deepseek-chat with diff edit format

Applied edit to auth/password.py
Tokens: 5.3k sent, 412 received. Cost: $0.0012 message, $0.0034 session.
"""


@pytest.fixture(scope="module")
def mirror_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """现建一套 golden 镜像，不动开发机上 `var/mirrors` 里那份。"""
    root = tmp_path_factory.mktemp("aider-mirrors")
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
        image="bench-agent:py311-aider",
        exit_code=exit_code,
        oom_killed=oom_killed,
        timed_out=timed_out,
        duration_s=1.0,
        stdout=stdout,
        stderr=stderr,
    )


class FakeContainer:
    """替掉 `run_in_container`：按脚本改工作区，返回事先写好的结果。

    顺手把 `ContainerSpec` 记下来 —— 挂载点、网络模式、workdir 这些不看一眼的话，
    等到真跑起来才发现挂错目录，一次调试要几分钟起步。
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
        self.edits = edits if edits is not None else {SOURCE_FILE: "# 被 aider 改过\n"}
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
) -> tuple[FakeContainer, object]:
    fake = FakeContainer(workspace, result=result, edits=edits)
    runner = AiderRunner(model="deepseek/deepseek-chat", run_container=fake)
    return fake, runner.run(task, workspace, config or AgentConfig())


# ── 补丁 ────────────────────────────────────────────────────


def test_patch_comes_from_the_workspace(workspace: Workspace, task: AgentTaskInput) -> None:
    """aider 改的是文件，补丁是我们跑 git diff 生成的（§9.1 的 workspace-mutation 模式）。

    这一条同时说明了为什么 `patch_source` 标 `git_diff`：这段 diff 是程序产出的，
    不是 AI 在 stdout 里打印的。后者行号常写错，归因时要能分开看。
    """
    _, result = run_with(workspace, task)
    assert result.has_patch
    assert SOURCE_FILE in result.patch
    assert result.patch_source == "git_diff"


def test_the_diff_is_taken_against_base_sha_not_head(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """就算 aider 自己提交了，补丁也要抓得到。

    `--no-auto-commits` 是我们给的，但真实 CLI 未必每条路径都听话。抓补丁走的是
    `git diff <base_sha>`，所以哪怕改动已经被提交进工作区的历史，diff 照样非空。
    换成裸 `git diff` 的话，这里会得到一个空补丁，然后这道题被判成"AI 什么都没做"。
    """
    from app.sandbox.git_cli import run_git

    def commit_like_a_disobedient_agent(spec: ContainerSpec) -> ContainerResult:
        (workspace.path / SOURCE_FILE).write_text("# 改完还自己提交了\n", encoding="utf-8")
        run_git(["add", "-A"], cwd=workspace.path, timeout_s=60)
        run_git(
            ["commit", "-m", "agent commit"],
            cwd=workspace.path,
            timeout_s=60,
            env_extra={
                "GIT_AUTHOR_NAME": "a",
                "GIT_AUTHOR_EMAIL": "a@b",
                "GIT_COMMITTER_NAME": "a",
                "GIT_COMMITTER_EMAIL": "a@b",
            },
        )
        return container_result()

    runner = AiderRunner(model="m", run_container=commit_like_a_disobedient_agent)
    result = runner.run(task, workspace, AgentConfig())
    assert result.has_patch
    assert SOURCE_FILE in result.patch


def test_protected_path_edits_stay_in_the_raw_patch(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """契约第 4 条：适配器交原始 diff，受保护路径的改动**留着**。

    方向和直觉相反。适配器自己过滤掉的话，`protected_path_edit_attempted`
    就没有证据了（协议 C-08b），而那是要触发人工复核的信号。
    过滤是平台在 E3-T3 做的事。
    """
    _, result = run_with(
        workspace,
        task,
        edits={SOURCE_FILE: "# 真修复\n", CHEAT_FILE: "# 把测试删了\n"},
    )
    assert CHEAT_FILE in result.patch


# ── 用量 ────────────────────────────────────────────────────


def test_token_and_cost_land_in_the_result(workspace: Workspace, task: AgentTaskInput) -> None:
    _, result = run_with(workspace, task)
    assert result.token_usage is not None
    assert result.token_usage.input == 5300
    assert result.token_usage.total == 5300 + 412
    assert result.cost_usd == pytest.approx(0.0034)
    assert result.cost_source is CostSource.REPORTED
    assert result.turns == 1


def test_unreadable_cost_is_reported_as_unavailable_not_zero(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """契约第 5 条 + 协议纪律 3：拿不到成本就说拿不到，不许拿 0 顶替。

    填 0 的话成本统计会悄悄偏低，而且事后从数据上分不出"真的没花钱"和"没读出来"。
    """
    _, result = run_with(workspace, task, result=container_result(stdout="Aider v0.86.2\n完事\n"))
    assert result.cost_usd is None
    assert result.cost_source is CostSource.UNAVAILABLE
    assert result.token_usage is None


def test_version_is_read_from_the_banner(workspace: Workspace, task: AgentTaskInput) -> None:
    """镜像里的版本和代码里那个常量可能不同步，以现场输出为准。"""
    stdout = "Aider v0.99.9\nTokens: 1k sent, 1 received.\n"
    _, result = run_with(workspace, task, result=container_result(stdout=stdout))
    assert result.agent_version == "0.99.9"


# ── 故障归类 ────────────────────────────────────────────────


def test_timeout_still_keeps_the_patch(workspace: Workspace, task: AgentTaskInput) -> None:
    """协议 C-09a：超时也要保存补丁，只是不跑测试。

    容器被墙钟杀掉时，改了一半的文件仍然留在工作区里。丢掉它等于把
    "AI 干到一半"和"AI 什么都没干"混成一种，而这两者的归因完全不同。
    """
    _, result = run_with(workspace, task, result=container_result(timed_out=True, exit_code=137))
    assert result.error is not None
    assert result.error.code == DEADLINE_EXCEEDED
    assert result.has_patch


def test_oom_is_not_reported_as_a_timeout(workspace: Workspace, task: AgentTaskInput) -> None:
    """两种情况退出码都是 137，判反了后果不同（协议 C-19b）。

    OOM 按 C-18 要降配重试；超时直接判 AI 没修好、不重试。所以先看 OOM 再看超时，
    顺序不能换。
    """
    _, result = run_with(
        workspace, task, result=container_result(oom_killed=True, timed_out=True, exit_code=137)
    )
    assert result.error is not None
    assert result.error.code == OOM_KILLED


def test_auth_failure_is_told_apart_from_a_crash(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """鉴权失败按 C-18 重试 3 次，运行时错误只重试 1 次，混为一谈会白跑。"""
    _, result = run_with(
        workspace,
        task,
        result=container_result(exit_code=1, stderr="litellm.AuthenticationError: 401"),
    )
    assert result.error is not None
    assert result.error.code == AUTH_FAILED


def test_the_error_message_keeps_the_useful_half(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """报错摘要必须带上 stdout。

    2026-09-05 实测：aider 的 stderr 里只有一句
    `Warning: Input is not a terminal (fd=0).`，真正的原因全在 stdout 上。
    早先那版先取 stderr，于是 `error_message_excerpt` 那一列里只剩这句废话，
    排查的人拿着它一点忙都帮不上。
    """
    _, result = run_with(
        workspace,
        task,
        edits={},
        result=container_result(
            stdout="litellm.RateLimitError: 说好的额度没了",
            stderr="Warning: Input is not a terminal (fd=0).",
            exit_code=0,
        ),
    )
    assert result.error is not None
    assert "RateLimitError" in result.error.message


def test_a_plain_crash_is_a_runtime_error(workspace: Workspace, task: AgentTaskInput) -> None:
    _, result = run_with(
        workspace, task, result=container_result(exit_code=2, stderr="Traceback ...")
    )
    assert result.error is not None
    assert result.error.code == RUNTIME_ERROR


def test_a_model_side_failure_is_caught_even_though_aider_exits_zero(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """**退出码 0 不等于跑成功。**

    2026-09-05 第一次真跑就撞上了：DeepSeek 的 Key 无效，aider 把
    `litellm.BadRequestError` 打在 stdout 上，然后正常退出，退出码 0。
    只看退出码的话，这一次会被记成"AI 跑完了但什么都没改"，判成 UNRESOLVED ——
    一个配错的 Key 就这样安静地把解决率拉到 0，排行榜上看不出任何异常。

    这段 stdout 是当时抓下来的原文，含 aider 自己的折行位置。
    """
    real_output = (
        "Aider v0.86.2\n"
        "Model: deepseek/deepseek-chat with diff edit format, prompt cache, infinite\n"
        "output\n"
        "Git repo: .git with 4 files\n"
        "\n"
        'litellm.BadRequestError: DeepseekException - {"error":{"message":"Authentication\n'
        "Fails, Your api key: ****9ca8 is\n"
        'invalid","type":"authentication_error","param":null,"code":"invalid_request_erro\n'
        'r"}}\n'
    )
    _, result = run_with(
        workspace, task, result=container_result(stdout=real_output, exit_code=0), edits={}
    )
    assert result.error is not None
    assert result.error.code == AUTH_FAILED


def test_an_empty_patch_with_exit_zero_is_not_a_failure(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """ "AI 没改出东西"是它自己的问题，对应 UNRESOLVED，不是平台故障。

    在这里报错的话，一次正常的"没修好"会被记成基础设施故障，然后触发重试 ——
    白花一次钱，还把失败归因引到错误的方向上。
    """
    _, result = run_with(workspace, task, edits={})
    assert result.error is None
    assert not result.has_patch


# ── 截止时刻 ────────────────────────────────────────────────


def test_a_passed_deadline_does_not_start_a_container(workspace: Workspace) -> None:
    """契约第 3 条：截止已过就优雅收手，且不留孤儿进程。

    最干净的实现是**根本没起过任何东西**。这里断言的就是 `run_container` 一次都没被调。
    """
    expired = make_task_input(deadline_ms=int(time.time() * 1000) - 1_000)
    fake, result = run_with(workspace, expired)
    assert fake.calls == 0
    assert result.error is not None
    assert result.error.code == DEADLINE_EXCEEDED


def test_a_deadline_too_close_to_be_useful_also_short_circuits(workspace: Workspace) -> None:
    """只剩几秒也别起容器。

    起一个容器、拉起 Python、装载 aider 本身就要十几秒。剩 5 秒钟去起容器，
    唯一确定的结果是白花一次启动时间，然后拿到一个超时。
    """
    barely = make_task_input(deadline_ms=int(time.time() * 1000) + 5_000)
    fake, _ = run_with(workspace, barely)
    assert fake.calls == 0


# ── 容器怎么起的 ────────────────────────────────────────────


def test_container_spec_matches_the_agent_stage(workspace: Workspace, task: AgentTaskInput) -> None:
    """挂载点、workdir、阶段标记这些一处错了都要真跑起来才发现，在这里先钉住。"""
    fake, _ = run_with(workspace, task)
    spec = fake.spec
    assert spec is not None
    assert spec.stage is Stage.AGENT
    assert spec.workdir == WORKSPACE_TARGET
    assert [m.source for m in spec.mounts] == [workspace.path]
    assert spec.command[0] == "aider"
    # run_id 用 task_id：事后按容器标签能直接对回是哪道题
    assert spec.run_id == task.task_id


def test_agent_stage_has_network_but_only_when_the_task_allows_it(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """被测 AI 要连大模型 API，所以 Agent 阶段联网。

    测试阶段永远断网（协议 C-31），那是测试执行器自己保证的，两边互不影响。
    """
    fake, _ = run_with(workspace, task)
    assert fake.spec is not None
    assert fake.spec.network is NetworkMode.BRIDGE

    offline = task.model_copy(
        update={"constraints": task.constraints.model_copy(update={"allow_network": False})}
    )
    fake_offline, _ = run_with(workspace, offline)
    assert fake_offline.spec is not None
    assert fake_offline.spec.network is NetworkMode.NONE


def test_env_and_image_come_from_the_agent_config(
    workspace: Workspace, task: AgentTaskInput
) -> None:
    """密钥和镜像都由 harness 侧的 `AgentConfig` 给，适配器不去读环境变量。

    适配器自己读 `os.environ` 的话，白名单（`build_env`）就形同虚设 ——
    那道白名单挡的是"把 GITHUB_TOKEN 也塞进被测 AI 的容器"这种事。
    """
    config = AgentConfig(
        image="bench-agent:custom", env={"DEEPSEEK_API_KEY": "sk-" + "test" + "0" * 20}
    )
    fake, _ = run_with(workspace, task, config=config)
    assert fake.spec is not None
    assert fake.spec.image == "bench-agent:custom"
    assert fake.spec.env["DEEPSEEK_API_KEY"].startswith("sk-")


# ── 制品 ────────────────────────────────────────────────────


def test_side_files_are_written_for_the_harness_to_pick_up(
    workspace: Workspace, task: AgentTaskInput, tmp_path: Path
) -> None:
    """全量 stdout 和轨迹写到 `artifact_dir`，由 `execute_task_run()` 存成制品。

    不能写工作区：写进去的文件会被 `git add -A` 收进补丁，变成"AI 改了几个文件"里
    多出来的那几个。
    """
    artifacts = tmp_path / "agent-io"
    _, result = run_with(workspace, task, config=AgentConfig(artifact_dir=artifacts))

    assert (artifacts / AGENT_STDOUT_FILENAME).read_text(encoding="utf-8") == SUCCESS_STDOUT
    assert (artifacts / AGENT_TRAJECTORY_FILENAME).exists()
    assert result.trajectory_uri is not None
    assert result.trajectory_uri.endswith(AGENT_TRAJECTORY_FILENAME)
    # 工作区里不许多出这些文件
    assert AGENT_STDOUT_FILENAME not in result.patch


def test_no_artifact_dir_is_fine(workspace: Workspace, task: AgentTaskInput) -> None:
    """哨兵和单测都不给 `artifact_dir`，不给就不写，也不该报错。"""
    _, result = run_with(workspace, task, config=AgentConfig())
    assert result.trajectory_uri is None


# ── 归因链路 ────────────────────────────────────────────────


def test_error_codes_are_the_canonical_ones(workspace: Workspace, task: AgentTaskInput) -> None:
    """适配器报的码必须能被 `_AGENT_ERROR_TO_INFRA` 查到。

    查不到的码一律落到 `AGENT_RUNTIME_ERROR`，也就是记在被测 AI 头上 ——
    一个拼错的错误码会让"容器 OOM"变成"AI 自己崩了"，而且没有任何报错提示。
    """
    from app.evaluation.task_run import _AGENT_ERROR_TO_INFRA

    cases = {
        container_result(timed_out=True): InfraOutcome.AGENT_TIMEOUT,
        container_result(oom_killed=True): InfraOutcome.OOM_KILLED,
        container_result(exit_code=1, stderr="401 unauthorized"): InfraOutcome.AGENT_AUTH_ERROR,
        container_result(exit_code=3): InfraOutcome.AGENT_RUNTIME_ERROR,
    }
    for container, expected in cases.items():
        _, result = run_with(workspace, task, result=container)
        assert result.error is not None
        assert _AGENT_ERROR_TO_INFRA[result.error.code] is expected
