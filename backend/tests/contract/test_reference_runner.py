"""拿一个参考适配器把契约套件跑通，再拿几个坏适配器证明它真的会红（E3-T1）。

分两半：

- **正向**：`TestReferenceRunner` 和 `TestSilentRunner` 继承 `AgentRunnerContract`，
  六条自动跑。这同时是给 E3-T2 和后面真实适配器看的用法示例——接一个新适配器
  只要写一个 `runner` fixture。
- **反向**：每个坏适配器只破坏一条契约，断言那一条必须抛 `AssertionError`。
  **没有反向这一半，一个永远返回"通过"的套件也能让正向全绿。**

参考适配器是**测试内的**，不是产品代码。真正的 Mock / Oracle / Noop 是 E3-T2。
这里只需要一个"什么都做对"的最小实现，用来证明契约本身能被满足。
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.enums import CostSource
from app.domain.protected_paths import is_protected
from app.runner.protocol import (
    AgentConfig,
    AgentError,
    AgentRunner,
    AgentRunResult,
    AgentTaskInput,
    ProbeResult,
    TokenUsage,
)
from tests.contract.runner_contract import PROTECTED_TARGET, AgentRunnerContract, child_pids

#: 参考适配器默认改的文件。普通源码路径，不受保护。
DEFAULT_TARGET = "src/app.py"

#: 反向用例里那个"截止已过还硬跑"的适配器要磨蹭多久。
#: 配一个很短的收尾窗口（`ImpatientContract`）之后，这条用例半秒就能跑完 ——
#: 照契约默认的 30 秒去写，`make check` 每次都要多等半分钟。
OVERRUN_SLEEP_S = 0.5
IMPATIENT_GRACE_S = 0.2


def fake_diff(path: str) -> str:
    """造一段最小但合法的 unified diff。契约第 2、4 条要从它里面解析出路径。"""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " def login(user, password):\n"
        "-    return True\n"
        "+    return bool(password) and password == user.password\n"
    )


class ReferenceRunner:
    """一个什么都做对的最小适配器。

    真实适配器要起容器、调 CLI、跑 `git diff`；这里把这些全省掉，只保留协议要求的
    行为：认 deadline、交原始补丁、如实报成本。
    """

    name = "reference"

    def __init__(self, *, target: str = DEFAULT_TARGET, patch: str | None = None) -> None:
        self._target = target
        self._patch = fake_diff(target) if patch is None else patch

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, agent_version="0.0.1", detail="测试用的参考适配器")

    def run(self, task: AgentTaskInput, workspace: Path, config: AgentConfig) -> AgentRunResult:
        started = datetime.now(UTC)
        expired = task.constraints.remaining_ms() <= 0
        finished = datetime.now(UTC)
        return AgentRunResult(
            agent_name=self.name,
            agent_version="0.0.1",
            model=task.model.name,
            started_at=started,
            finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000),
            # 截止时刻已过就立刻收手，交空手。这是协议里 deadline 的语义：
            # 软预算，让适配器自己体面地停下来，不是等 harness 来强杀
            patch="" if expired else self._patch,
            error=(
                AgentError(code="deadline_exceeded", message="截止时刻已过，没开工就收手")
                if expired
                else None
            ),
            token_usage=TokenUsage(input=1200, output=340),
            cost_usd=0.0012,
            cost_source=CostSource.REPORTED,
            turns=3,
        )


# ── 正向：契约套件跑得通 ────────────────────────────────────


class TestReferenceRunner(AgentRunnerContract):
    """接一个新适配器长什么样：给一个 `runner` fixture，六条自动跑。"""

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return ReferenceRunner()

    def runner_that_edits(self, relative_path: str) -> AgentRunner:
        """可编程的假适配器给得出第 4 条要的场景，真实 CLI 适配器多半给不出。"""
        return ReferenceRunner(target=relative_path)


class TestSilentRunner(AgentRunnerContract):
    """交空补丁的适配器（Noop 哨兵就是这个形状）也要能过契约。

    `produces_patch = False` 是一句**声明**，不是免检。第 2 条会反过来要求它
    确实交空补丁——一个声明了 False 却交出补丁的适配器同样会红。
    """

    produces_patch = False

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return ReferenceRunner(patch="")


# ── 反向：把适配器弄坏，套件必须红 ──────────────────────────


class MuteProbeRunner(ReferenceRunner):
    """探活失败却不说原因。等于没有 probe——只知道"不行"，不知道为什么不行。"""

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=False)


class OverrunRunner(ReferenceRunner):
    """截止时刻已过还接着磨蹭。"""

    def run(self, task: AgentTaskInput, workspace: Path, config: AgentConfig) -> AgentRunResult:
        if task.constraints.remaining_ms() <= 0:
            time.sleep(OVERRUN_SLEEP_S)
        return super().run(task, workspace, config)


class OrphanRunner(ReferenceRunner):
    """起了子进程却不收，跑完留下孤儿。"""

    def __init__(self) -> None:
        super().__init__()
        self.leaked: subprocess.Popen[bytes] | None = None

    def run(self, task: AgentTaskInput, workspace: Path, config: AgentConfig) -> AgentRunResult:
        self.leaked = subprocess.Popen(["sleep", "30"])
        return super().run(task, workspace, config)


class BadTokenRunner(ReferenceRunner):
    """token 总数比单项还小。数字自相矛盾，成本估算会跟着错。"""

    def run(self, task: AgentTaskInput, workspace: Path, config: AgentConfig) -> AgentRunResult:
        result = super().run(task, workspace, config)
        return result.model_copy(update={"token_usage": TokenUsage(input=100, output=50, total=10)})


class FilteringRunner(ReferenceRunner):
    """自作主张把受保护路径的改动过滤掉，抹掉了作弊证据（协议 C-08b）。"""

    def run(self, task: AgentTaskInput, workspace: Path, config: AgentConfig) -> AgentRunResult:
        result = super().run(task, workspace, config)
        return result.model_copy(update={"patch": fake_diff(DEFAULT_TARGET)})


class ImpatientContract(AgentRunnerContract):
    """收尾窗口很短的契约，专门给反向的超时用例用。"""

    deadline_grace_s = IMPATIENT_GRACE_S


class SilentContract(AgentRunnerContract):
    """声明"这个适配器不产出补丁"的契约。"""

    produces_patch = False


class FilteringContract(AgentRunnerContract):
    """第 4 条的场景由一个会自己过滤的坏适配器提供。"""

    def runner_that_edits(self, relative_path: str) -> AgentRunner:
        return FilteringRunner()


def test_mute_probe_is_caught(tmp_path: Path) -> None:
    """探活失败却不说原因，第 1 条必须红。"""
    with pytest.raises(AssertionError, match="没给原因"):
        AgentRunnerContract().test_probe_reports_availability(MuteProbeRunner())


def test_missing_patch_is_caught(tmp_path: Path) -> None:
    """声明会交补丁却交了空手，第 2 条必须红。"""
    with pytest.raises(AssertionError):
        AgentRunnerContract().test_produces_patch_on_a_solvable_task(
            ReferenceRunner(patch=""), tmp_path, AgentConfig()
        )


def test_unexpected_patch_is_caught(tmp_path: Path) -> None:
    """反过来也一样：声明了 `produces_patch = False` 却交出补丁，同样红。

    这条防的是拿声明当免检——把 `produces_patch` 改成 False 并不能让第 2 条闭嘴。
    """
    with pytest.raises(AssertionError):
        SilentContract().test_produces_patch_on_a_solvable_task(
            ReferenceRunner(), tmp_path, AgentConfig()
        )


def test_deadline_overrun_is_caught(tmp_path: Path) -> None:
    """截止已过还磨蹭超过收尾窗口，第 3 条必须红。"""
    with pytest.raises(AssertionError, match="截止已过"):
        ImpatientContract().test_returns_gracefully_when_the_deadline_has_passed(
            OverrunRunner(), tmp_path, AgentConfig()
        )


def test_orphan_process_is_caught(tmp_path: Path) -> None:
    """留下孤儿进程，第 3 条必须红。

    `/proc` 读不到子进程时（非 Linux）这条查不了，直接跳过而不是假装通过。
    """
    if not Path("/proc/self/task").is_dir():
        pytest.skip("没有 /proc，查不了子进程")

    runner = OrphanRunner()
    try:
        with pytest.raises(AssertionError, match="孤儿进程"):
            AgentRunnerContract().test_returns_gracefully_when_the_deadline_has_passed(
                runner, tmp_path, AgentConfig()
            )
    finally:
        if runner.leaked is not None:
            runner.leaked.kill()
            runner.leaked.wait(timeout=5)
    assert runner.leaked is not None and runner.leaked.pid not in child_pids()


def test_inconsistent_token_usage_is_caught(tmp_path: Path) -> None:
    """token 总数小于单项，第 5 条必须红。"""
    with pytest.raises(AssertionError):
        AgentRunnerContract().test_cost_is_reported_or_explicitly_unavailable(
            BadTokenRunner(), tmp_path, AgentConfig()
        )


def test_adapter_side_filtering_is_caught(tmp_path: Path) -> None:
    """适配器自己过滤受保护路径，第 4 条必须红。

    这是六条里最反直觉的一条：过滤本身是对的，但**不能由适配器做**，
    否则 `protected_path_edit_attempted` 就没有证据了（协议 C-08b）。
    """
    with pytest.raises(AssertionError, match="自己过滤"):
        FilteringContract().test_protected_path_edits_stay_in_the_raw_patch(tmp_path, AgentConfig())


def test_missing_scenario_skips_instead_of_passing(tmp_path: Path) -> None:
    """给不出第 4 条的场景时是 skip，不是悄悄通过。

    默认实现返回 None。要是写成"给不出就算过"，真实适配器接进来时这条会静默失效，
    看起来六条全绿，实际只跑了五条。
    """
    with pytest.raises(pytest.skip.Exception):
        AgentRunnerContract().test_protected_path_edits_stay_in_the_raw_patch(
            tmp_path, AgentConfig()
        )


def test_protected_target_is_actually_protected() -> None:
    """契约里用的那个路径必须真的命中默认清单，否则第 4 条测了个寂寞。"""
    assert is_protected(PROTECTED_TARGET)
