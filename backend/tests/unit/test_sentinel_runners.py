"""三个哨兵适配器的单元测试（E3-T2）。

这里只验**不碰磁盘就能验的东西**：六种行为交出来的补丁长什么样、配置怎么解析、
结果里的字段自不自洽。"这个补丁到底打不打得上""Oracle 是不是真能解决题目"
要真的物化工作区、真的跑 pytest，在 `tests/sandbox/test_sentinel_golden.py`。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.domain.enums import CostSource
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import enforcement_patterns, is_protected, protected_hits
from app.runner.adapters import MockBehavior, MockRunner, NoopRunner, OracleRunner
from app.runner.adapters.base import GOLD_PATCH_MISSING
from app.runner.adapters.mock import (
    DEADLINE_EXCEEDED,
    DEFAULT_PROTECTED_TARGET,
    MOCK_SOURCE_PATH,
)
from app.runner.protocol import AgentConfig, AgentRunner, AgentRunResult, AgentTaskInput
from tests.contract.runner_contract import make_task_input

TASK_ID = "bench-contract__demo-1"
GOLD = (
    "diff --git a/src/auth.py b/src/auth.py\n"
    "--- a/src/auth.py\n"
    "+++ b/src/auth.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def login(user, password):\n"
    "-    return True\n"
    "+    return bool(password) and password == user.password\n"
)


def live_task(*, extra_ms: int = 60_000) -> AgentTaskInput:
    """一份还没到期的任务输入。"""
    return make_task_input(task_id=TASK_ID, deadline_ms=int(time.time() * 1000) + extra_ms)


def expired_task() -> AgentTaskInput:
    """一份截止时刻已经过去的任务输入。"""
    return make_task_input(task_id=TASK_ID, deadline_ms=int(time.time() * 1000) - 1_000)


def run(runner: AgentRunner, task: AgentTaskInput, tmp_path: Path) -> AgentRunResult:
    return runner.run(task, tmp_path, AgentConfig())


# ── 三个哨兵都满足协议 ──────────────────────────────────────


@pytest.mark.parametrize(
    "runner",
    [NoopRunner(), OracleRunner(), MockRunner()],
    ids=["noop", "oracle", "mock"],
)
def test_sentinels_satisfy_the_runner_protocol(runner: object) -> None:
    """`AgentRunner` 是 runtime_checkable 的，这条能当场验出少写了哪个方法。"""
    assert isinstance(runner, AgentRunner)


@pytest.mark.parametrize(
    "runner",
    [NoopRunner(), OracleRunner({TASK_ID: GOLD}), MockRunner(patches={TASK_ID: GOLD})],
    ids=["noop", "oracle", "mock"],
)
def test_sentinels_report_zero_cost_not_unknown_cost(runner: AgentRunner, tmp_path: Path) -> None:
    """成本报的是"确实是 0"，不是"不知道"。

    协议纪律 3 里"不要填 0"针对的是拿不到成本的情况。哨兵不调任何模型，
    它的成本就是 0，而且我们知道它是 0 —— 这时候标 `unavailable` 反而会让
    报表把一笔已知的 0 当成待估算的空洞。
    """
    result = run(runner, live_task(), tmp_path)
    assert result.cost_source is CostSource.REPORTED
    assert result.cost_usd == 0.0
    assert result.token_usage is not None and result.token_usage.total == 0
    assert result.model is None, "哨兵一个 token 都没发出去，不该在结果里挂一个模型名"


# ── Noop ────────────────────────────────────────────────────


def test_noop_always_returns_an_empty_patch(tmp_path: Path) -> None:
    """空补丁，而且不带错误——交空手是 Noop 的定义，不是它出了故障。"""
    result = run(NoopRunner(), live_task(), tmp_path)
    assert result.patch == ""
    assert result.error is None
    assert result.exit_code == 0
    assert result.agent_name == "noop"


def test_noop_output_does_not_depend_on_the_task(tmp_path: Path) -> None:
    """换一道题、换一个已过期的 deadline，输出必须一模一样。

    Noop 是解决率的基线。基线一旦随输入变化，"0%"这个结论就不再是常量了。
    """
    other = make_task_input(task_id="acme__web-42", deadline_ms=int(time.time() * 1000) + 5)
    first = run(NoopRunner(), live_task(), tmp_path)
    second = run(NoopRunner(), other, tmp_path)
    third = run(NoopRunner(), expired_task(), tmp_path)
    assert first.patch == second.patch == third.patch == ""


# ── Oracle ──────────────────────────────────────────────────


def test_oracle_hands_back_the_gold_patch_verbatim(tmp_path: Path) -> None:
    """一个字节都不改。改了的话，"Oracle 解决率 100%"验的就不是官方补丁了。"""
    result = run(OracleRunner({TASK_ID: GOLD}), live_task(), tmp_path)
    assert result.patch == GOLD
    assert result.error is None


def test_oracle_ignores_the_deadline(tmp_path: Path) -> None:
    """截止时刻已过也照样交出官方补丁。

    这一条是刻意的，理由在 `oracle.py` 的 `run()` 里：Oracle 的输出只由 task_id
    决定，编排层哪天算错一次 deadline，也不该让"假阴性探针"跟着失灵。
    """
    result = run(OracleRunner({TASK_ID: GOLD}), expired_task(), tmp_path)
    assert result.patch == GOLD


def test_oracle_accepts_a_lookup_function(tmp_path: Path) -> None:
    """题多的时候可以喂一个按需查的函数，不必先把几百份补丁读进内存。"""
    result = run(
        OracleRunner(lambda task_id: GOLD if task_id == TASK_ID else None), live_task(), tmp_path
    )
    assert result.patch == GOLD


def test_oracle_without_the_patch_fails_loudly(tmp_path: Path) -> None:
    """查不到补丁 → 空补丁 + 明确的错误码 + 非零退出码。"""
    result = run(OracleRunner({"another__task-1": GOLD}), live_task(), tmp_path)
    assert result.patch == ""
    assert result.exit_code == 1
    assert result.error is not None and result.error.code == GOLD_PATCH_MISSING
    assert TASK_ID in result.error.message, "报错里要说清是哪道题缺补丁"


# ── Mock：六种行为 ──────────────────────────────────────────


def test_mock_correct_patch_hands_back_the_gold_patch(tmp_path: Path) -> None:
    result = run(
        MockRunner(MockBehavior.CORRECT_PATCH, patches={TASK_ID: GOLD}), live_task(), tmp_path
    )
    assert result.patch == GOLD


def test_mock_correct_patch_without_a_patch_fails_loudly(tmp_path: Path) -> None:
    """和 Oracle 同一个错误码：报表里"补丁没喂进来"只有一种写法。"""
    result = run(MockRunner(MockBehavior.CORRECT_PATCH), live_task(), tmp_path)
    assert result.patch == ""
    assert result.exit_code == 1
    assert result.error is not None and result.error.code == GOLD_PATCH_MISSING


def test_mock_wrong_patch_is_a_real_diff_that_touches_nothing_protected(tmp_path: Path) -> None:
    """错误补丁：解析得出路径、碰不到受保护路径、和官方补丁不是一个东西。"""
    result = run(
        MockRunner(MockBehavior.WRONG_PATCH, patches={TASK_ID: GOLD}), live_task(), tmp_path
    )
    paths = derive_patch_paths(result.patch)

    assert paths == [MOCK_SOURCE_PATH]
    assert not protected_hits(tuple(paths), enforcement_patterns())
    assert result.patch != GOLD
    assert result.error is None, "交了个修不好的补丁不是适配器故障，是被测 AI 没做对"


def test_mock_empty_patch_is_quiet(tmp_path: Path) -> None:
    """空补丁行为不报错——它模拟的是"AI 悄悄放弃了"，不是"AI 超时了"。

    这两种要分开：前者判 EMPTY_PATCH，后者判 AGENT_TIMEOUT，责任归属一样但
    归因分析要看的东西完全不同。
    """
    result = run(MockRunner(MockBehavior.EMPTY_PATCH), live_task(), tmp_path)
    assert result.patch == ""
    assert result.error is None


def test_mock_timeout_reports_the_deadline_and_returns_empty(tmp_path: Path) -> None:
    result = run(MockRunner(MockBehavior.TIMEOUT), live_task(), tmp_path)
    assert result.patch == ""
    assert result.error is not None and result.error.code == DEADLINE_EXCEEDED


def test_mock_timeout_sleeps_no_longer_than_the_cap(tmp_path: Path) -> None:
    """deadline 还早的时候也不能真等到那时候。

    golden 题的 `agent_timeout_s` 是 300，不设上限的话一条测试要跑五分钟。
    """
    runner = MockRunner(MockBehavior.TIMEOUT, max_sleep_s=0.05)
    started = time.monotonic()
    run(runner, live_task(extra_ms=300_000), tmp_path)
    assert time.monotonic() - started < 1.0


def test_mock_timeout_returns_at_once_when_the_deadline_has_passed(tmp_path: Path) -> None:
    """截止已过就一秒不睡，契约第 3 条靠这条行为过关。"""
    runner = MockRunner(MockBehavior.TIMEOUT, max_sleep_s=5.0)
    started = time.monotonic()
    result = run(runner, expired_task(), tmp_path)
    assert time.monotonic() - started < 1.0
    assert result.error is not None and result.error.code == DEADLINE_EXCEEDED


def test_mock_malformed_patch_is_non_empty_and_still_parses(tmp_path: Path) -> None:
    """非法补丁必须**非空**且解析得出路径。

    协议里 INVALID_PATCH 的定义是"补丁非空但打不上去"。交一段自然语言是另一种
    失败（适配器根本没交出补丁），走的是别的判定分支，不该混进来。
    真的打不上这一条要在真工作区上验，见 tests/sandbox/test_sentinel_golden.py。
    """
    result = run(MockRunner(MockBehavior.MALFORMED_PATCH), live_task(), tmp_path)
    assert result.has_patch
    assert derive_patch_paths(result.patch) == [MOCK_SOURCE_PATH]


def test_mock_protected_path_edit_keeps_the_evidence(tmp_path: Path) -> None:
    """改测试文件的那一半必须留在原始补丁里（协议 C-08b、契约第 4 条）。

    同时还得有一半是真的源码改动：只造作弊的那一半，E3-T3 的归一化就没有
    "该留的留下、该剔的剔掉"可测了。
    """
    result = run(MockRunner(MockBehavior.PROTECTED_PATH_EDIT), live_task(), tmp_path)
    paths = derive_patch_paths(result.patch)

    assert DEFAULT_PROTECTED_TARGET in paths
    assert MOCK_SOURCE_PATH in paths
    assert is_protected(DEFAULT_PROTECTED_TARGET)
    assert not is_protected(MOCK_SOURCE_PATH)


def test_mock_protected_target_is_configurable(tmp_path: Path) -> None:
    """契约第 4 条要指定改哪个文件，靠的就是这个参数。"""
    runner = MockRunner(MockBehavior.PROTECTED_PATH_EDIT, protected_target="tests/test_sample.py")
    result = run(runner, live_task(), tmp_path)
    assert "tests/test_sample.py" in derive_patch_paths(result.patch)


def test_all_six_behaviors_are_distinguishable(tmp_path: Path) -> None:
    """六种行为交出来的东西两两不同。

    这条防的是"配置项加了，行为没接上"——两种行为悄悄产出同一个结果的话，
    上面每条用例单看都是绿的，只有横着比才看得出来。
    """
    outputs = {
        behavior: run(MockRunner(behavior, patches={TASK_ID: GOLD}), live_task(), tmp_path)
        for behavior in MockBehavior
    }
    assert len(outputs) == 6

    patches = {behavior: result.patch for behavior, result in outputs.items()}
    # 空补丁和超时都交空手，靠 error 区分；其余四种的补丁内容两两不同
    assert patches[MockBehavior.EMPTY_PATCH] == patches[MockBehavior.TIMEOUT] == ""
    assert outputs[MockBehavior.EMPTY_PATCH].error is None
    assert outputs[MockBehavior.TIMEOUT].error is not None

    with_patch = [
        p for b, p in patches.items() if b not in (MockBehavior.EMPTY_PATCH, MockBehavior.TIMEOUT)
    ]
    assert len(set(with_patch)) == 4


# ── Mock：从配置构造 ────────────────────────────────────────


def test_from_params_reads_the_behavior_name() -> None:
    runner = MockRunner.from_params({"behavior": "wrong_patch"})
    assert runner.behavior is MockBehavior.WRONG_PATCH


def test_from_params_defaults_to_the_correct_patch() -> None:
    """不配 behavior 就走"正确补丁"。默认值挑它是因为它最无害：
    一个配置漏了的实验会跑出满分，一眼就能看出不对；默认成 timeout 反而像真故障。"""
    assert MockRunner.from_params({}).behavior is MockBehavior.CORRECT_PATCH


def test_from_params_rejects_an_unknown_behavior() -> None:
    """行为名拼错要当场炸，不能静默退回默认值。

    静默退回的话，一个写成 `timout` 的配置会让整批实验安安静静跑成"正确补丁"，
    结果看上去完全正常——这类 bug 最难查。
    """
    with pytest.raises(ValueError, match="不认识的 Mock 行为"):
        MockRunner.from_params({"behavior": "timout"})


def test_from_params_reads_per_task_overrides() -> None:
    """逐题指定行为，才凑得出一次"有的解决、有的超时"的混合实验。"""
    runner = MockRunner.from_params(
        {"behavior": "correct_patch", "per_task": {"acme__web-42": "timeout"}}
    )
    assert runner.behavior_for("acme__web-42") is MockBehavior.TIMEOUT
    assert runner.behavior_for("acme__web-1") is MockBehavior.CORRECT_PATCH


def test_from_params_rejects_a_malformed_per_task() -> None:
    with pytest.raises(ValueError, match="per_task"):
        MockRunner.from_params({"per_task": ["acme__web-42"]})


def test_per_task_behavior_actually_changes_the_output(tmp_path: Path) -> None:
    """`behavior_for` 说了算的东西，`run()` 要真的照着做。

    只测 `behavior_for` 的话，`run()` 里忘了调它也照样绿。
    """
    runner = MockRunner(
        MockBehavior.CORRECT_PATCH,
        patches={TASK_ID: GOLD},
        per_task={TASK_ID: MockBehavior.EMPTY_PATCH},
    )
    assert run(runner, live_task(), tmp_path).patch == ""
