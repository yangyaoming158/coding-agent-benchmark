"""重试与 canonical attempt 的选取规则（E5-T1，协议 C-24、C-53、C-58、C-71）。

不碰数据库、不碰 Docker —— 规则本身是纯函数，所以协议里的每一条边界都能在这里
写成一条断言。这也是把它放进 `app.domain` 的原因。

## 为什么值得写这么多条

canonical attempt 是解决率的取数依据。选错了不会报错、不会有日志，
只会让排行榜上的百分比悄悄偏掉。这类 bug 只能靠穷举边界来防。
"""

from __future__ import annotations

import pytest

from app.domain.enums import InfraOutcome
from app.domain.protocol import INFRA_TO_AGENT_MAPPING, MAX_ATTEMPTS_PER_TASK, is_retryable
from app.domain.retry import (
    STOP_GLOBAL_CAP,
    STOP_NON_RETRYABLE,
    STOP_TYPE_BUDGET_EXHAUSTED,
    AttemptRecord,
    decide_next,
    first_non_retryable,
    type_budget_left,
)


def history(*outcomes: InfraOutcome) -> list[AttemptRecord]:
    """按顺序造一段 attempt 历史，编号从 1 开始。"""
    return [AttemptRecord(attempt_no=i, infra_outcome=o) for i, o in enumerate(outcomes, start=1)]


# ── C-24 上半句：第一个不可重试的结果直接定案 ────────────────


def test_success_is_canonical_immediately() -> None:
    """跑成功了当然不用再跑。`SUCCESS` 的自动重试次数是 0，走的是同一条规则。"""
    decision = decide_next(history(InfraOutcome.SUCCESS))
    assert decision.should_retry is False
    assert decision.canonical_attempt_no == 1
    assert decision.stop_reason == STOP_NON_RETRYABLE


@pytest.mark.parametrize(
    "outcome",
    [InfraOutcome.AGENT_TIMEOUT, InfraOutcome.PATCH_APPLY_FAILED, InfraOutcome.CANCELLED],
)
def test_non_retryable_failures_stop_at_once(outcome: InfraOutcome) -> None:
    """C-18 里自动重试次数为 0 的故障，一次就定案。

    这三种的共同点是"再跑一次也是一样的结果"：AI 自己超时、补丁本身就打不上、
    人工取消。重试它们只是白烧机时。
    """
    assert INFRA_TO_AGENT_MAPPING[outcome].max_auto_retries == 0
    decision = decide_next(history(outcome))
    assert decision.should_retry is False
    assert decision.canonical_attempt_no == 1


def test_canonical_is_not_the_largest_attempt_no() -> None:
    """C-58 的反例：第 1 次就 `AGENT_TIMEOUT`，它就是认定结果。

    协议明确**禁止**用"取最大的 attempt_no"来推断 canonical。这条历史里
    第 2 条编号更大，但按 C-24 该算数的是第 1 条。取最大编号会算错。

    （按我们自己的调度逻辑第 2 条根本不会产生，但 E5-T2 的人工重跑会造出来，
    协议也正是为此专门写了 C-58。）
    """
    records = history(InfraOutcome.AGENT_TIMEOUT, InfraOutcome.SANDBOX_ERROR)
    decision = decide_next(records)
    assert decision.canonical_attempt_no == 1
    assert first_non_retryable(records) is records[0]


# ── C-24 下半句：全可重试时，取重试耗尽后的最后一次 ─────────


def test_retryable_failure_schedules_another_attempt() -> None:
    """`ENV_BUILD_FAILED` 可以重试 1 次，所以第一次失败后要再排一次。"""
    decision = decide_next(history(InfraOutcome.ENV_BUILD_FAILED))
    assert decision.should_retry is True
    assert decision.canonical_attempt_no is None
    assert decision.next_attempt_no == 2


def test_same_failure_twice_exhausts_its_budget() -> None:
    """同一种故障连着来两次，`max_auto_retries=1` 的预算就用完了。"""
    decision = decide_next(history(InfraOutcome.ENV_BUILD_FAILED, InfraOutcome.ENV_BUILD_FAILED))
    assert decision.should_retry is False
    assert decision.canonical_attempt_no == 2
    assert decision.stop_reason == STOP_TYPE_BUDGET_EXHAUSTED


def test_sandbox_error_gets_two_retries() -> None:
    """`SANDBOX_ERROR` 在 C-18 里是 2 次，所以第 3 次才停。"""
    assert INFRA_TO_AGENT_MAPPING[InfraOutcome.SANDBOX_ERROR].max_auto_retries == 2
    assert decide_next(history(InfraOutcome.SANDBOX_ERROR)).should_retry is True
    two = history(InfraOutcome.SANDBOX_ERROR, InfraOutcome.SANDBOX_ERROR)
    assert decide_next(two).should_retry is True
    three = [*two, AttemptRecord(3, InfraOutcome.SANDBOX_ERROR)]
    assert decide_next(three).should_retry is False
    assert decide_next(three).canonical_attempt_no == 3


# ── C-71：全局上限 ──────────────────────────────────────────


def test_mixed_failures_are_capped_globally() -> None:
    """C-71 的原话场景：每种错误各自的预算都没超，但这道题不能一直跑下去。

    `ENV_BUILD_FAILED`（1 次）→ `SANDBOX_ERROR`（2 次）→ `SANDBOX_ERROR`
    → `OOM_KILLED`（1 次）：走到第 4 次时，OOM 自己的预算还剩着，
    但 attempt 总数已经到 `MAX_ATTEMPTS_PER_TASK`，必须停。

    不设这个上限，重试预算会被不同错误类型轮流重置。
    """
    assert MAX_ATTEMPTS_PER_TASK == 4
    records = history(
        InfraOutcome.ENV_BUILD_FAILED,
        InfraOutcome.SANDBOX_ERROR,
        InfraOutcome.SANDBOX_ERROR,
        InfraOutcome.OOM_KILLED,
    )
    # 第 4 次这个故障自己的预算其实还有富余 —— 全局上限才是拦住它的那一条
    assert type_budget_left(records, InfraOutcome.OOM_KILLED) > 0

    decision = decide_next(records)
    assert decision.should_retry is False
    assert decision.canonical_attempt_no == 4
    assert decision.stop_reason == STOP_GLOBAL_CAP


def test_three_mixed_failures_still_retry() -> None:
    """上一条的前一步：只跑了 3 次，还没到上限，该继续。"""
    decision = decide_next(
        history(
            InfraOutcome.ENV_BUILD_FAILED,
            InfraOutcome.SANDBOX_ERROR,
            InfraOutcome.SANDBOX_ERROR,
        )
    )
    assert decision.should_retry is True
    assert decision.next_attempt_no == 4


def test_auth_error_budget_meets_the_global_cap_exactly() -> None:
    """`AGENT_AUTH_ERROR` 允许重试 3 次，加上第一次正好 4 次，和全局上限齐平。

    这条卡的是"两个上限哪个先生效"：类型预算没超，但总数到了，
    停止原因必须记成全局上限——排查时这两者指向完全不同的问题。
    """
    assert INFRA_TO_AGENT_MAPPING[InfraOutcome.AGENT_AUTH_ERROR].max_auto_retries == 3
    records = history(*([InfraOutcome.AGENT_AUTH_ERROR] * 4))
    decision = decide_next(records)
    assert decision.should_retry is False
    assert decision.stop_reason == STOP_GLOBAL_CAP


# ── 一致性：规则只能从 C-18 的表里长出来 ────────────────────


@pytest.mark.parametrize("outcome", list(InfraOutcome))
def test_every_outcome_produces_a_decision(outcome: InfraOutcome) -> None:
    """13 种 `infra_outcome` 每一种都要能做出决定，不能有漏网的。

    漏一种的话，那种故障会让作业停在既不重试也没有 canonical 的状态，
    这道题就永远结束不了，而且没有任何报错。
    """
    decision = decide_next(history(outcome))
    assert decision.should_retry == is_retryable(outcome)
    assert (decision.canonical_attempt_no is None) == decision.should_retry


def test_empty_history_is_a_programming_error() -> None:
    """没跑过就来问"要不要重试"，是调用方写错了，不能静默返回一个默认值。"""
    with pytest.raises(ValueError, match="不能为空"):
        decide_next([])
