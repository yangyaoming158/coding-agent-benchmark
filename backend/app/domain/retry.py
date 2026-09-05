"""重试与 canonical attempt 的选取规则（协议 C-24、C-53、C-58、C-71）。

这里是**纯函数**：输入一道题目前所有 attempt 的故障结果，输出"还要不要再跑一次、
哪一次算数"。不碰数据库，不碰 Docker，所以可以把协议里的每一条边界都写成单测。

## 为什么要单独一个模块

canonical attempt（认定结果）是解决率的取数依据。选错了，排行榜就是错的，
而且错得很安静——没有异常、没有日志，只有一个偏低或偏高的百分比。

放在 domain 层是因为有三个地方要用同一份判断，而它们互相看不见：

- Worker 的 EVAL_TASK 处理函数（跑完一次之后决定要不要再排一次）
- E5-T2 的编排层（失败重跑）
- E10-T3 的报表（复核历史数据里的 canonical 标记对不对）

## 两条规则

**C-24：canonical = 第一个不可重试的结果；全都可重试就取重试耗尽后的最后一次。**

"不可重试"查的是 C-18 映射表里 `max_auto_retries == 0`（`is_retryable()` 就是这个），
`SUCCESS` 也算——它没什么好重试的。

**C-58 禁止"取最大的 attempt_no"。** canonical 不一定是编号最大的那条：
第 1 次就 `AGENT_TIMEOUT`（不可重试）的话，它就是认定结果，哪怕后面因为别的
原因又产生了记录（比如人工在 E5-T2 里触发过重跑）。

## 重试预算怎么算

C-18 是**按错误类型**分别规定次数的，所以预算也按类型独立计数：看最后一次的
故障类型 X，数一数整个历史里出现过几次 X，`次数 ≤ max_auto_retries(X)` 就还能再来。

光有这个不够，所以还有 C-71 的全局上限（`MAX_ATTEMPTS_PER_TASK`，4）：一道题先
`ENV_BUILD_FAILED`、再 `SANDBOX_ERROR`、再 `OOM_KILLED`，每种类型各自的预算都没超，
但这道题已经跑了一堆次了。不设全局上限，预算会被不同错误类型轮流重置。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.enums import InfraOutcome
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    MAX_ATTEMPTS_PER_TASK,
    is_retryable,
)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """历史里的一次 attempt。只取选 canonical 用得上的两个字段。

    刻意不用 ORM 对象：这个模块在 domain 层，看不见 `EvaluationTaskRun`，
    而且这样单测里造历史只要写一行。
    """

    attempt_no: int
    infra_outcome: InfraOutcome


class RetryStop(str):
    """不再重试的原因。是字符串子类，直接往日志和 `last_error` 里塞就行。"""

    __slots__ = ()


#: 停止重试的四种原因。写成常量而不是散落的字面量，报表要按它分组统计。
STOP_NON_RETRYABLE = RetryStop("non_retryable")
STOP_TYPE_BUDGET_EXHAUSTED = RetryStop("type_budget_exhausted")
STOP_GLOBAL_CAP = RetryStop("global_attempt_cap")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """跑完一次之后的决定。

    两个字段互斥：`should_retry` 为真时 `canonical_attempt_no` 必然是 None
    （还没定下来哪次算数），为假时必然有值。
    """

    should_retry: bool
    #: 哪一次 attempt 算数（协议 C-24）。还要重试时为 None。
    canonical_attempt_no: int | None
    #: 下一次的编号。不重试时为 None。
    next_attempt_no: int | None
    #: 为什么停。还要重试时为 None。写进日志，事后能回答"这题为什么只跑了两次"。
    stop_reason: RetryStop | None

    def __post_init__(self) -> None:
        if self.should_retry != (self.canonical_attempt_no is None):
            raise ValueError("should_retry 与 canonical_attempt_no 必须互斥")


def first_non_retryable(history: Sequence[AttemptRecord]) -> AttemptRecord | None:
    """历史里第一个不可重试的 attempt，没有就返回 None。

    单独抽出来是因为 C-24 和 C-58 说的是同一件事的正反面：canonical 优先取它，
    而**禁止**改用"编号最大的那条"。
    """
    for record in history:
        if not is_retryable(record.infra_outcome):
            return record
    return None


def type_budget_left(history: Sequence[AttemptRecord], outcome: InfraOutcome) -> int:
    """`outcome` 这个故障还剩几次重试机会（协议 C-18 的"自动重试"列）。

    按类型独立计数：数整个历史里出现过几次 `outcome`。第一次不算重试，
    所以剩余次数 = `max_auto_retries - (出现次数 - 1)`。
    """
    seen = sum(1 for record in history if record.infra_outcome is outcome)
    return INFRA_TO_AGENT_MAPPING[outcome].max_auto_retries - (seen - 1)


def decide_next(history: Sequence[AttemptRecord]) -> RetryDecision:
    """跑完一次之后：还要不要再来一次？哪一次算数？

    `history` 按 attempt 顺序排列，至少一条。**只由 `infra_outcome` 决定**——
    协议 C-53 禁止用"这次不太对，再跑一遍"这种人工判断触发自动重试。
    """
    if not history:
        raise ValueError("history 不能为空：至少要有一次跑完的 attempt 才能做决定")

    # C-24 第一句：第一个不可重试的结果直接定案。
    # 注意这里查的是整个历史，不是最后一次 —— C-58 的反例正是"第 1 次就
    # AGENT_TIMEOUT，后面又冒出了别的记录"，那时候最后一次不是 canonical。
    decisive = first_non_retryable(history)
    if decisive is not None:
        return RetryDecision(
            should_retry=False,
            canonical_attempt_no=decisive.attempt_no,
            next_attempt_no=None,
            stop_reason=STOP_NON_RETRYABLE,
        )

    last = history[-1]
    # C-71：全局上限优先于类型预算。先判它，否则不同错误类型会轮流重置预算。
    if len(history) >= MAX_ATTEMPTS_PER_TASK:
        return RetryDecision(
            should_retry=False,
            canonical_attempt_no=last.attempt_no,
            next_attempt_no=None,
            stop_reason=STOP_GLOBAL_CAP,
        )

    if type_budget_left(history, last.infra_outcome) <= 0:
        return RetryDecision(
            should_retry=False,
            canonical_attempt_no=last.attempt_no,
            next_attempt_no=None,
            stop_reason=STOP_TYPE_BUDGET_EXHAUSTED,
        )

    return RetryDecision(
        should_retry=True,
        canonical_attempt_no=None,
        next_attempt_no=last.attempt_no + 1,
        stop_reason=None,
    )


__all__ = [
    "STOP_GLOBAL_CAP",
    "STOP_NON_RETRYABLE",
    "STOP_TYPE_BUDGET_EXHAUSTED",
    "AttemptRecord",
    "RetryDecision",
    "RetryStop",
    "decide_next",
    "first_non_retryable",
    "type_budget_left",
]
